#!/usr/bin/env python3
"""
Unified AI CLI Manager

Goals:
1. One-command install for Claude Code CLI, Codex CLI, Gemini CLI, and Qwen Code CLI.
2. Shared skills hub with provider-specific mounts/symlinks.
3. Terminal command to inspect local conversation/session storage paths.

This script is intentionally provider-adapter based so new CLIs can be added easily.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

def add_to_user_path(new_path: str) -> None:
    if not is_windows():
        return

    import winreg

    normalized_new = str(Path(new_path)).strip().lower()

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Environment",
        0,
        winreg.KEY_ALL_ACCESS,
    ) as key:
        try:
            current, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current = ""

        parts = [p.strip() for p in current.split(";") if p.strip()]
        normalized_parts = [p.lower() for p in parts]

        if normalized_new not in normalized_parts:
            parts.append(str(Path(new_path)))
            new_value = ";".join(parts)
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_value)

    os.environ["PATH"] = str(Path(new_path)) + os.pathsep + os.environ.get("PATH", "")
# -----------------------------------------------------------------------------
# Core paths
# -----------------------------------------------------------------------------
HOME = Path.home()
SYSTEM = platform.system().lower()
AI_HUB = HOME / ".ai-cli-hub"
SHARED_SKILLS_ROOT = AI_HUB / "shared-skills"
LOCAL_BIN = AI_HUB / "bin"


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
def is_windows() -> bool:
    return SYSTEM == "windows"


def is_macos() -> bool:
    return SYSTEM == "darwin"


def is_linux() -> bool:
    return SYSTEM == "linux"


def print_header(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")


if is_windows():
    import winreg
else:
    winreg = None
    
def run(cmd: Sequence[str] | str, *, shell: bool = False, check: bool = True) -> int:
    print(f"[run] {cmd if isinstance(cmd, str) else ' '.join(cmd)}")
    try:
        completed = subprocess.run(cmd, shell=shell, check=check)
        return completed.returncode
    except subprocess.CalledProcessError as exc:
        print(f"[error] command failed with exit code {exc.returncode}", file=sys.stderr)
        if check:
            raise
        return exc.returncode


def which(binary: str) -> str | None:
    return shutil.which(binary)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_text_if_changed(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.write_text(content, encoding="utf-8")


def safe_remove(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def copy_dir_contents(src: Path, dst: Path) -> None:
    safe_remove(dst)
    shutil.copytree(src, dst)


def symlink_or_copy_dir(src: Path, dst: Path) -> str:
    """Try symlink first; on Windows fall back to directory junction; finally copy."""
    safe_remove(dst)
    ensure_dir(dst.parent)

    try:
        os.symlink(src, dst, target_is_directory=True)
        return "symlink"
    except OSError:
        pass

    if is_windows():
        try:
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return "junction"
        except Exception:
            pass

    copy_dir_contents(src, dst)
    return "copy"


def find_recent_files(root: Path, patterns: Sequence[str], limit: int = 8) -> list[Path]:
    if not root.exists():
        return []
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(root.rglob(pattern))
    uniq = sorted({p.resolve() for p in matches if p.exists()}, key=lambda p: p.stat().st_mtime, reverse=True)
    return uniq[:limit]


def bootstrap_local_bin() -> None:
    ensure_dir(LOCAL_BIN)
    source = Path(__file__).resolve()

    if is_windows():
        manager_copy = LOCAL_BIN / "aicli.py"
        internal_ps1 = LOCAL_BIN / "_aicli.ps1"
        public_cmd = LOCAL_BIN / "aicli.cmd"

        safe_remove(LOCAL_BIN / "aicli.ps1")
        safe_remove(internal_ps1)
        safe_remove(public_cmd)

        shutil.copy2(source, manager_copy)

        ps1_content = textwrap.dedent(
            f"""\
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CliArgs
)

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) {{
    & $python.Source "{str(manager_copy)}" @CliArgs
    exit $LASTEXITCODE
}}

$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {{
    & $py.Source "{str(manager_copy)}" @CliArgs
    exit $LASTEXITCODE
}}

Write-Error "python or py not found in PATH"
exit 1
"""
        )

        cmd_content = textwrap.dedent(
            """\
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_aicli.ps1" %*
"""
        )

        write_text_if_changed(internal_ps1, ps1_content)
        write_text_if_changed(public_cmd, cmd_content)
        return

    manager_py = LOCAL_BIN / "aicli.py"
    launcher_sh = LOCAL_BIN / "aicli"

    shutil.copy2(source, manager_py)

    sh_content = textwrap.dedent(
        f"""\
#!/usr/bin/env bash
set -e
exec python3 "{str(manager_py)}" "$@"
"""
    )

    write_text_if_changed(launcher_sh, sh_content)
    launcher_sh.chmod(0o755)
    manager_py.chmod(0o755)


def path_instructions() -> str:
    if is_windows():
        return (
            f"把 {LOCAL_BIN} 加到 PATH。\n"
            f"PowerShell 临时生效：\n"
            f"  $env:Path += ';{LOCAL_BIN}'\n"
            f"永久生效：已写入用户 PATH，重新打开终端后生效。\n"
            f"之后可用：aicli install --with-node"
        )

    shell_rc = "~/.bashrc 或 ~/.zshrc"
    return (
        f"把 {LOCAL_BIN} 加到 PATH。追加到 {shell_rc}：\n"
        f"  export PATH=\"{LOCAL_BIN}:$PATH\"\n"
        f"然后重新打开终端。之后可用：aicli install --with-node"
    )

def build_proxy_env(
    host: str,
    port: int,
    protocol: str = "http",
    socks5_port: int | None = None,
) -> dict[str, str]:
    http_proxy = f"http://{host}:{port}"
    https_proxy = f"http://{host}:{port}"

    env = {
        "HTTP_PROXY": http_proxy,
        "HTTPS_PROXY": https_proxy,
        "http_proxy": http_proxy,
        "https_proxy": https_proxy,
    }

    if socks5_port is not None:
        all_proxy = f"socks5://{host}:{socks5_port}"
        env["ALL_PROXY"] = all_proxy
        env["all_proxy"] = all_proxy

    return env

def render_proxy_commands(shell_name: str, envs: dict[str, str]) -> str:
    shell_name = shell_name.lower()

    if shell_name in ("powershell", "pwsh"):
        parts = [f"$env:{k}='{v}'" for k, v in envs.items()]
        return "; ".join(parts)

    if shell_name == "cmd":
        lines = [f"set {k}={v}" for k, v in envs.items()]
        return "\n".join(lines)

    if shell_name in ("bash", "zsh", "sh"):
        lines = [f'export {k}="{v}"' for k, v in envs.items()]
        return "\n".join(lines)

    raise ValueError(f"unsupported shell: {shell_name}")
    
def render_clear_proxy_commands(shell_name: str, keys: list[str] | None = None) -> str:
    keys = keys or [
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ]
    shell_name = shell_name.lower()

    if shell_name in ("powershell", "pwsh"):
        parts = [f"Remove-Item Env:{k} -ErrorAction SilentlyContinue" for k in keys]
        return "; ".join(parts)

    if shell_name == "cmd":
        return "\n".join([f"set {k}=" for k in keys])

    if shell_name in ("bash", "zsh", "sh"):
        return "\n".join([f"unset {k}" for k in keys])

    raise ValueError(f"unsupported shell: {shell_name}")

def exec_with_proxy(
    command: Sequence[str],
    host: str,
    port: int,
    socks5_port: int | None = None,
) -> int:
    env = os.environ.copy()
    env.update(build_proxy_env(host, port, socks5_port=socks5_port))

    print(f"[proxy] 为命令注入代理: {host}:{port}")
    print(f"[run] {' '.join(command)}")

    completed = subprocess.run(command, env=env)
    return completed.returncode
    
# -----------------------------------------------------------------------------
# Installation helpers
# -----------------------------------------------------------------------------
def ensure_node(with_install: bool) -> None:
    if which("node") and which("npm"):
        print("[ok] Node.js/npm 已存在")
        return

    if not with_install:
        raise RuntimeError(
            "缺少 Node.js/npm。请重新运行并加 --with-node，或手工安装 Node.js LTS。"
        )

    print_header("安装 Node.js")
    if is_windows():
        if not which("winget"):
            raise RuntimeError("Windows 下未检测到 winget，无法自动安装 Node.js。")
        run(["winget", "install", "-e", "--id", "OpenJS.NodeJS.LTS", "--accept-source-agreements", "--accept-package-agreements"])
    elif is_macos():
        if not which("brew"):
            raise RuntimeError("macOS 下未检测到 Homebrew，无法自动安装 Node.js。")
        run(["brew", "install", "node"])
    elif is_linux():
        if which("apt-get"):
            run(["sudo", "apt-get", "update"])
            run(["sudo", "apt-get", "install", "-y", "nodejs", "npm"])
        elif which("dnf"):
            run(["sudo", "dnf", "install", "-y", "nodejs", "npm"])
        elif which("pacman"):
            run(["sudo", "pacman", "-Sy", "--noconfirm", "nodejs", "npm"])
        else:
            raise RuntimeError("Linux 下未检测到 apt/dnf/pacman，无法自动安装 Node.js。")
    else:
        raise RuntimeError(f"不支持的系统: {SYSTEM}")


# -----------------------------------------------------------------------------
# Provider adapters
# -----------------------------------------------------------------------------
@dataclass
class Provider:
    name: str
    binary: str
    installer: Callable[[bool], None]
    skill_mounts: Callable[[], list[Path]]
    conversation_roots: Callable[[], list[tuple[str, Path, Sequence[str]]]]
    notes: str = ""
    verify_cmd: Sequence[str] = field(default_factory=list)

    def is_installed(self) -> bool:
        return which(self.binary) is not None

    def install(self, with_node: bool) -> None:
        self.installer(with_node)


def install_claude(_: bool) -> None:
    print_header("安装 Claude Code CLI")
    if is_windows():
        ps = "irm https://claude.ai/install.ps1 | iex"
        run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
    else:
        run("curl -fsSL https://claude.ai/install.sh | bash", shell=True)


def install_codex(with_node: bool) -> None:
    print_header("安装 Codex CLI")
    ensure_node(with_node)
    run(["npm", "install", "-g", "@openai/codex"])


def install_gemini(with_node: bool) -> None:
    print_header("安装 Gemini CLI")
    ensure_node(with_node)
    run(["npm", "install", "-g", "@google/gemini-cli"])


def install_qwen(with_node: bool) -> None:
    print_header("安装 Qwen Code CLI")
    ensure_node(with_node)
    run(["npm", "install", "-g", "@qwen-code/qwen-code@latest"])


def claude_skill_mounts() -> list[Path]:
    return [HOME / ".claude" / "skills"]


def codex_skill_mounts() -> list[Path]:
    return [HOME / ".agents" / "skills"]


def gemini_skill_mounts() -> list[Path]:
    return [HOME / ".gemini" / "extensions" / "skills-hub" / "skills"]


def qwen_skill_mounts() -> list[Path]:
    return [HOME / ".qwen" / "skills"]


def claude_roots() -> list[tuple[str, Path, Sequence[str]]]:
    return [
        ("transcripts", HOME / ".claude" / "projects", ["*.jsonl"]),
        ("prompt_history", HOME / ".claude", ["history.jsonl"]),
    ]


def codex_roots() -> list[tuple[str, Path, Sequence[str]]]:
    codex_home = Path(os.environ.get("CODEX_HOME", str(HOME / ".codex")))
    return [
        ("sessions", codex_home / "sessions", ["*.jsonl"]),
        ("archived_sessions", codex_home / "archived_sessions", ["*.jsonl"]),
        ("prompt_history", codex_home, ["history.jsonl"]),
    ]


def gemini_roots() -> list[tuple[str, Path, Sequence[str]]]:
    base = HOME / ".gemini"
    return [
        ("saved_chat_checkpoints_and_tmp", base / "tmp", ["*", "*.json", "*.md"]),
        ("shell_history", base / "tmp", ["shell_history"]),
    ]


def qwen_roots() -> list[tuple[str, Path, Sequence[str]]]:
    base = HOME / ".qwen"
    return [
        ("history", base / "history", ["*.json", "*.jsonl", "*.md"]),
        ("tmp", base / "tmp", ["shell_history", "*.json", "*.jsonl"]),
        ("projects", base / "projects", ["*.json", "*.jsonl", "*.md"]),
    ]


PROVIDERS: dict[str, Provider] = {
    "claude": Provider(
        name="claude",
        binary="claude",
        installer=install_claude,
        skill_mounts=claude_skill_mounts,
        conversation_roots=claude_roots,
        verify_cmd=["claude", "--version"],
        notes="Claude Code 会把项目会话转录保存在 ~/.claude/projects/.../*.jsonl。",
    ),
    "codex": Provider(
        name="codex",
        binary="codex",
        installer=install_codex,
        skill_mounts=codex_skill_mounts,
        conversation_roots=codex_roots,
        verify_cmd=["codex", "--version"],
        notes="Codex 会把本地会话写到 $CODEX_HOME/sessions（默认 ~/.codex/sessions）。",
    ),
    "gemini": Provider(
        name="gemini",
        binary="gemini",
        installer=install_gemini,
        skill_mounts=gemini_skill_mounts,
        conversation_roots=gemini_roots,
        verify_cmd=["gemini", "--version"],
        notes="Gemini 官方文档明确给出 /chat 保存点和 shell_history 位于 ~/.gemini/tmp/<project_hash>/。自动全量对话转录路径未稳定公开，所以这里只报告可审计的 checkpoint/tmp 路径。",
    ),
    "qwen": Provider(
        name="qwen",
        binary="qwen",
        installer=install_qwen,
        skill_mounts=qwen_skill_mounts,
        conversation_roots=qwen_roots,
        verify_cmd=["qwen", "--version"],
        notes="Qwen 运行时历史与临时文件默认在 ~/.qwen 下。",
    ),
}


# -----------------------------------------------------------------------------
# Skill management
# -----------------------------------------------------------------------------
DEFAULT_SKILL_TEMPLATE = textwrap.dedent(
    """
    ---
    name: {name}
    description: {description}
    ---

    # {title}

    目标：
    - 在合适场景下使用这个 skill
    - 遵循这里定义的步骤、检查项和输出格式

    触发条件：
    - TODO: 写清楚什么时候应该触发

    不应触发：
    - TODO: 写清楚什么时候不应该触发

    工作步骤：
    1. TODO
    2. TODO
    3. TODO

    输出要求：
    - TODO
    """
).strip() + "\n"


def ensure_gemini_extension_manifest() -> None:
    ext_root = HOME / ".gemini" / "extensions" / "skills-hub"
    ensure_dir(ext_root)
    manifest = {
        "name": "skills-hub",
        "version": "1.0.0",
        "description": "Shared skills mounted from ~/.ai-cli-hub/shared-skills",
    }
    write_text_if_changed(ext_root / "gemini-extension.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


def sync_skills(providers: Iterable[Provider] | None = None) -> None:
    providers = list(providers or PROVIDERS.values())
    ensure_dir(SHARED_SKILLS_ROOT)
    ensure_gemini_extension_manifest()

    shared_skills = [p for p in SHARED_SKILLS_ROOT.iterdir() if p.is_dir() and (p / "SKILL.md").exists()]
    if not shared_skills:
        print("[warn] 共享 skill 目录为空：", SHARED_SKILLS_ROOT)
        return

    for provider in providers:
        for mount_root in provider.skill_mounts():
            ensure_dir(mount_root)
            print_header(f"同步 {provider.name} skills -> {mount_root}")
            for skill_dir in shared_skills:
                dst = mount_root / skill_dir.name
                mode = symlink_or_copy_dir(skill_dir, dst)
                print(f"[ok] {skill_dir.name} -> {dst} ({mode})")


def add_skill(name: str, description: str) -> Path:
    skill_dir = SHARED_SKILLS_ROOT / name
    ensure_dir(skill_dir)
    title = name.replace("-", " ").title()
    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists():
        raise RuntimeError(f"skill 已存在: {skill_md}")
    content = DEFAULT_SKILL_TEMPLATE.format(name=name, description=description, title=title)
    write_text_if_changed(skill_md, content)
    return skill_dir


def list_skills() -> list[str]:
    ensure_dir(SHARED_SKILLS_ROOT)
    names = []
    for item in sorted(SHARED_SKILLS_ROOT.iterdir()):
        if item.is_dir() and (item / "SKILL.md").exists():
            names.append(item.name)
    return names


# -----------------------------------------------------------------------------
# Conversation/session path inspection
# -----------------------------------------------------------------------------

def show_paths(selected: Sequence[str]) -> int:
    for key in selected:
        provider = PROVIDERS[key]
        print_header(f"{provider.name} 会话/历史路径")
        print(provider.notes)
        roots = provider.conversation_roots()
        for label, root, patterns in roots:
            print(f"\n[{label}] {root}")
            if not root.exists():
                print("  - 路径不存在")
                continue
            recent = find_recent_files(root, patterns)
            if not recent:
                print("  - 未发现匹配文件")
                continue
            for path in recent:
                print(f"  - {path}")
    return 0


# -----------------------------------------------------------------------------
# Install workflow
# -----------------------------------------------------------------------------

def install_all(selected: Sequence[str], with_node: bool) -> int:
    ensure_dir(SHARED_SKILLS_ROOT)

    for key in selected:
        provider = PROVIDERS[key]
        print_header(f"安装 {provider.name}")
        provider.install(with_node)

    print_header("安装验证")
    failures = 0
    for key in selected:
        provider = PROVIDERS[key]
        if provider.is_installed():
            print(f"[ok] {provider.name}: {which(provider.binary)}")
            if provider.verify_cmd:
                try:
                    run(provider.verify_cmd, check=False)
                except Exception:
                    pass
        else:
            failures += 1
            print(f"[fail] {provider.name}: 未检测到 {provider.binary} in PATH", file=sys.stderr)

    sync_skills([PROVIDERS[k] for k in selected])

    if failures:
        print("\n有安装项未通过 PATH 验证。通常是终端未刷新或 npm global bin 尚未进入 PATH。", file=sys.stderr)
        return 1
    return 0


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_provider_args(values: Sequence[str] | None) -> list[str]:
    if not values or values == ["all"]:
        return list(PROVIDERS.keys())
    invalid = [v for v in values if v not in PROVIDERS]
    if invalid:
        raise SystemExit(f"不支持的 provider: {', '.join(invalid)}")
    return list(values)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aicli",
        description="统一安装和管理 Claude/Codex/Gemini/Qwen CLI，以及共享 skills。",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install", help="安装一个或多个 CLI")
    p_install.add_argument("providers", nargs="*", default=["all"], help="claude codex gemini qwen all")
    p_install.add_argument("--with-node", action="store_true", help="Node.js/npm 缺失时自动安装")

    p_sync = sub.add_parser("sync-skills", help="把共享 skills 同步到各 CLI")
    p_sync.add_argument("providers", nargs="*", default=["all"], help="claude codex gemini qwen all")

    p_add = sub.add_parser("add-skill", help="创建一个共享 skill")
    p_add.add_argument("name", help="skill 名称，建议 kebab-case")
    p_add.add_argument("description", help="skill 的触发描述")

    sub.add_parser("list-skills", help="列出共享 skills")

    p_paths = sub.add_parser("paths", help="显示各 CLI 的会话/历史路径")
    p_paths.add_argument("providers", nargs="*", default=["all"], help="claude codex gemini qwen all")

    sub.add_parser("bootstrap", help="把 aicli 自身复制到 ~/.ai-cli-hub/bin")
    
    p_proxy = sub.add_parser("proxy", help="生成或使用代理环境")
    
    proxy_sub = p_proxy.add_subparsers(dest="proxy_cmd", required=True)

    p_proxy_print = proxy_sub.add_parser("print", help="打印当前 shell 可执行的代理设置命令")
    p_proxy_print.add_argument("--host", default="127.0.0.1")
    p_proxy_print.add_argument("--port", type=int, required=True)
    p_proxy_print.add_argument("--socks5-port", type=int)
    p_proxy_print.add_argument("--shell", choices=["powershell", "cmd", "bash", "zsh", "sh"], required=True)

    p_proxy_clear = proxy_sub.add_parser("clear-print", help="打印清理代理的命令")
    p_proxy_clear.add_argument("--shell", choices=["powershell", "cmd", "bash", "zsh", "sh"], required=True)

    p_proxy_exec = proxy_sub.add_parser("exec", help="仅对某条命令临时注入代理")
    p_proxy_exec.add_argument("--host", default="127.0.0.1")
    p_proxy_exec.add_argument("--port", type=int, required=True)
    p_proxy_exec.add_argument("--socks5-port", type=int)
    p_proxy_exec.add_argument("command", nargs=argparse.REMAINDER, help="要执行的命令，前面加 --")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "bootstrap":
        bootstrap_local_bin()
        add_to_user_path(str(LOCAL_BIN))
        print(path_instructions())
        return 0

    if args.cmd == "install":
        providers = parse_provider_args(args.providers)
        return install_all(providers, args.with_node)

    if args.cmd == "sync-skills":
        providers = parse_provider_args(args.providers)
        sync_skills([PROVIDERS[k] for k in providers])
        return 0

    if args.cmd == "add-skill":
        path = add_skill(args.name, args.description)
        print(f"[ok] 已创建: {path}")
        print("接下来请编辑 SKILL.md，然后运行: aicli sync-skills")
        return 0

    if args.cmd == "list-skills":
        for name in list_skills():
            print(name)
        return 0

    if args.cmd == "paths":
        providers = parse_provider_args(args.providers)
        return show_paths(providers)

    if args.cmd == "proxy":
        if args.proxy_cmd == "print":
            envs = build_proxy_env(args.host, args.port, socks5_port=args.socks5_port)
            print(render_proxy_commands(args.shell, envs))
            return 0

        if args.proxy_cmd == "clear-print":
            print(render_clear_proxy_commands(args.shell))
            return 0

        if args.proxy_cmd == "exec":
            cmd = list(args.command)
            if cmd and cmd[0] == "--":
                cmd = cmd[1:]
            if not cmd:
                print("缺少要执行的命令，例如: aicli proxy exec --port 7890 -- gemini", file=sys.stderr)
                return 2
            return exec_with_proxy(cmd, args.host, args.port, socks5_port=args.socks5_port)
        
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
