#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汇英斋数据库终端（Windows 远程版）。

通过 SSH 连接服务器，把 dbterm.py 上传到服务器后远程运行：
所有数据库操作都在服务器上完成，服务器上的 Linux 版本不需要任何修改。

本地只做三件事：
  1. 读取 remote.env
  2. 把 dbterm.py 上传到服务器（内容未变时跳过）
  3. 通过 ssh -t 打开远端终端 / 执行单条命令

用法：
    dbterm.bat                    进入远程交互终端
    dbterm.bat status             在服务器执行一条命令
    dbterm.bat --check            检查 SSH、sudo、数据库是否可用
    dbterm.bat --deploy           只上传最新的 dbterm.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

VERSION = "1.0.0"

TOOL_DIR = Path(__file__).resolve().parent
LOCAL_SCRIPT = TOOL_DIR / "dbterm.py"
CONFIG_FILE = TOOL_DIR / "remote.env"
CONFIG_EXAMPLE = TOOL_DIR / "remote.env.example"
CACHE_FILE = TOOL_DIR / ".remote-cache.json"

MISSING_EXIT_CODE = 97

DEFAULTS = {
    "HOST": "",
    "USER": "root",
    "PORT": "22",
    "SSH_KEY": "",
    "SSH_OPTIONS": "",
    "SSH_BIN": "",
    "SCP_BIN": "",
    "SERVER_ROOT": "/opt/huiyingzhai/server",
    "SUDO_USER": "huiyingzhai",
    "PYTHON": "python3",
    "REMOTE_SCRIPT": "/tmp/huiyingzhai-dbterm.py",
    "REMOTE_HOME": "/opt/huiyingzhai/.dbterm",
}

FALLBACK_EXAMPLE = """# 汇英斋数据库终端 —— Windows 远程服务器配置
# 此文件含主机信息，已被 .gitignore 忽略，不会被提交。

HOST=api.example.com
USER=root
PORT=22

# SSH 私钥路径；留空则使用 ssh 默认密钥或交互输入密码
# SSH_KEY=C:\\Users\\你的用户名\\.ssh\\id_rsa
# 额外 ssh 参数，例如首次连接自动接受指纹
# SSH_OPTIONS=-o StrictHostKeyChecking=accept-new

# 服务端目录（install.sh 默认位置）
SERVER_ROOT=/opt/huiyingzhai/server

# 以哪个用户身份执行。留空表示直接用 SSH 用户执行；通常填 huiyingzhai
SUDO_USER=huiyingzhai

# 服务器上的 Python 解释器
PYTHON=python3

# 终端脚本上传位置（/tmp 重启后会清空，下次运行会自动重新上传）
REMOTE_SCRIPT=/tmp/huiyingzhai-dbterm.py

# 远程备份与导出目录，避免写进 /tmp：backups/、exports/ 会放在这里
REMOTE_HOME=/opt/huiyingzhai/.dbterm
"""


class ConfigError(Exception):
    """配置或连接错误，直接展示给用户。"""


def error(text: str) -> None:
    print(f"[错误] {text}", file=sys.stderr)


def info(text: str) -> None:
    print(text)


# ===== 配置 =====
def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise ConfigError(f"读取配置失败：{path}（{exc}）") from exc
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip().upper()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def open_editor(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass


def prepare_config_file(path: Path) -> None:
    """首次运行时生成 remote.env 并用记事本打开。"""
    if path != CONFIG_FILE:
        raise ConfigError(f"配置文件不存在：{path}")
    if CONFIG_EXAMPLE.exists():
        text = CONFIG_EXAMPLE.read_text(encoding="utf-8-sig")
    else:
        text = FALLBACK_EXAMPLE
    path.write_text(text, encoding="utf-8")
    info(f"已生成配置文件：{path}")
    info("请填写 HOST、USER（推荐配置 SSH 密钥），保存后重新运行。")
    open_editor(path)


def split_command(raw: str) -> list[str]:
    """按空白拆分命令片段，保留 Windows 反斜杠。"""
    try:
        tokens = shlex.split(raw, posix=False)
    except ValueError as exc:
        raise ConfigError(f"命令格式错误：{raw}（{exc}）") from exc
    cleaned = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        cleaned.append(token)
    return cleaned


def load_config(path: Path) -> dict[str, str]:
    if not path.exists():
        prepare_config_file(path)
        raise ConfigError("已生成 remote.env，请填写 HOST / USER 后重新运行。")

    values = {**DEFAULTS, **parse_env_file(path)}

    host = values["HOST"].strip()
    user = values["USER"].strip()
    if not host or host in ("api.example.com", "example.com", "你的域名"):
        raise ConfigError("请先在 remote.env 中填写 HOST（服务器 IP 或 SSH 别名）。")
    if not user:
        raise ConfigError("remote.env 缺少 USER。")

    try:
        port = int(values["PORT"].strip() or "22")
    except ValueError as exc:
        raise ConfigError("PORT 必须是整数。") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("PORT 应在 1-65535 之间。")

    server_root = values["SERVER_ROOT"].strip()
    if server_root and not server_root.startswith("/"):
        raise ConfigError("SERVER_ROOT 必须是服务器上的绝对路径，例如 /opt/huiyingzhai/server。")

    remote_script = values["REMOTE_SCRIPT"].strip()
    if not remote_script.startswith("/"):
        raise ConfigError("REMOTE_SCRIPT 必须是服务器上的绝对路径。")
    if any(char.isspace() for char in remote_script):
        raise ConfigError("REMOTE_SCRIPT 不能包含空格。")

    remote_home = values["REMOTE_HOME"].strip()
    if remote_home and not remote_home.startswith("/"):
        raise ConfigError("REMOTE_HOME 必须是服务器上的绝对路径（或留空）。")

    key = values["SSH_KEY"].strip()
    if key:
        key_path = Path(key).expanduser()
        if not key_path.exists():
            raise ConfigError(f"SSH 私钥不存在：{key_path}")
        key = str(key_path)

    values["HOST"] = host
    values["USER"] = user
    values["PORT"] = str(port)
    values["SSH_KEY"] = key
    values["SERVER_ROOT"] = server_root
    values["REMOTE_SCRIPT"] = remote_script
    values["REMOTE_HOME"] = remote_home
    values["SUDO_USER"] = values["SUDO_USER"].strip()
    values["PYTHON"] = values["PYTHON"].strip() or "python3"
    values["SSH_OPTIONS"] = values["SSH_OPTIONS"].strip()
    return values


def binary_command(
    config: dict[str, str], key: str, default: str, *, required: bool = True
) -> Optional[list[str]]:
    raw = config.get(key) or default
    parts = split_command(raw)
    if not parts:
        if required:
            raise ConfigError(f"未配置 {key}。")
        return None
    executable = parts[0]
    resolved = shutil.which(executable)
    if not resolved and Path(executable).exists():
        resolved = executable
    if not resolved:
        if required:
            raise ConfigError(
                f"未找到 {executable}。Windows 10/11 请安装「OpenSSH 客户端」：\n"
                "  设置 → 应用 → 可选功能 → 添加功能 → OpenSSH 客户端"
            )
        return None
    return [resolved] + parts[1:]


def ssh_base(config: dict[str, str], *, batch: bool = False) -> list[str]:
    command = binary_command(config, "SSH_BIN", "ssh")
    assert command is not None
    command += ["-p", config["PORT"]]
    if config["SSH_KEY"]:
        command += ["-i", config["SSH_KEY"]]
    if config["SSH_OPTIONS"]:
        command += split_command(config["SSH_OPTIONS"])
    if batch:
        command += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    return command


def scp_base(config: dict[str, str], *, batch: bool = False) -> Optional[list[str]]:
    command = binary_command(config, "SCP_BIN", "scp", required=False)
    if command is None:
        return None
    command += ["-P", config["PORT"]]
    if config["SSH_KEY"]:
        command += ["-i", config["SSH_KEY"]]
    if config["SSH_OPTIONS"]:
        command += split_command(config["SSH_OPTIONS"])
    if batch:
        command += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    return command


def target(config: dict[str, str]) -> str:
    return f"{config['USER']}@{config['HOST']}"


# ===== 上传 / 缓存 =====
def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_cache() -> dict[str, Any]:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict[str, Any]) -> None:
    try:
        CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def cache_key(config: dict[str, str]) -> str:
    return (
        f"{config['USER']}@{config['HOST']}:{config['PORT']}"
        f"|{config['REMOTE_SCRIPT']}"
    )


def upload_script(
    config: dict[str, str],
    *,
    force: bool = False,
    quiet: bool = False,
    batch: bool = False,
) -> bool:
    """上传本地 dbterm.py；内容未变化时跳过。返回是否真的上传。"""
    if not LOCAL_SCRIPT.exists():
        raise ConfigError(f"缺少 {LOCAL_SCRIPT}，请确认 db-terminal 目录完整。")

    digest = file_sha256(LOCAL_SCRIPT)
    key = cache_key(config)
    cache = load_cache()
    if not force and cache.get(key) == digest:
        return False

    remote_script = config["REMOTE_SCRIPT"]
    remote_target = f"{target(config)}:{remote_script}"
    if not quiet:
        info(f"正在上传 dbterm.py 到 {config['HOST']}:{remote_script} ...")

    scp = scp_base(config, batch=batch)
    if scp:
        command = scp + [str(LOCAL_SCRIPT), remote_target]
        try:
            code = subprocess.call(command)
        except OSError as exc:
            raise ConfigError(f"启动 scp 失败：{exc}") from exc
    else:
        # 没有 scp 时通过 SSH 标准输入写入
        command = ssh_base(config, batch=batch) + [
            target(config),
            f"cat > {shlex.quote(remote_script)}",
        ]
        try:
            with LOCAL_SCRIPT.open("rb") as handle:
                code = subprocess.call(command, stdin=handle)
        except OSError as exc:
            raise ConfigError(f"上传失败：{exc}") from exc

    if code != 0:
        raise ConfigError(
            f"上传失败（退出码 {code}）。请检查 HOST/USER/密钥，"
            f"以及服务器上的 {remote_script} 是否可写。"
        )

    cache[key] = digest
    save_cache(cache)
    if not quiet:
        info("上传完成。")
    return True


# ===== 远程执行 =====
def remote_shell_command(config: dict[str, str], args: list[str]) -> str:
    parts: list[str] = []
    if config["SUDO_USER"]:
        parts += ["sudo", "-u", config["SUDO_USER"]]
    if config["REMOTE_HOME"]:
        parts += ["env", f"DBTERM_HOME={config['REMOTE_HOME']}"]
    python_parts = split_command(config["PYTHON"]) or ["python3"]
    parts += python_parts
    parts += [config["REMOTE_SCRIPT"]]
    if config["SERVER_ROOT"]:
        parts += ["--server-root", config["SERVER_ROOT"]]
    parts += args
    return " ".join(shlex.quote(part) for part in parts)


def wrap_precheck(config: dict[str, str], command: str) -> str:
    """先确认远程脚本存在，缺失时返回专属退出码供本地自动重传。"""
    script = shlex.quote(config["REMOTE_SCRIPT"])
    return (
        f"test -f {script} || exit {MISSING_EXIT_CODE}; "
        f"chmod 644 {script} 2>/dev/null || true; "
        f"{command}"
    )


def run_remote(
    config: dict[str, str], args: list[str], *, tty: bool = True, retry: bool = True
) -> int:
    command = wrap_precheck(config, remote_shell_command(config, args))
    ssh_command = ssh_base(config)
    if tty:
        ssh_command.append("-t")
    ssh_command += [target(config), command]

    try:
        code = subprocess.call(ssh_command)
    except OSError as exc:
        raise ConfigError(f"启动 ssh 失败：{exc}") from exc

    if code == MISSING_EXIT_CODE and retry:
        info("远程终端文件不存在，正在重新上传 ...")
        upload_script(config, force=True)
        return run_remote(config, args, tty=tty, retry=False)

    if code == 255:
        error("SSH 连接失败（返回码 255），请检查 HOST、USER、网络和密钥。")
    return code


# ===== 命令行 =====
def print_help() -> None:
    print(
        f"""汇英斋数据库终端 · Windows 远程版 v{VERSION}

通过 SSH 在服务器上运行 dbterm.py，所有数据库操作都在服务器完成。
配置写在 remote.env（首次运行会自动生成并打开记事本）。

准备工作：
  1. Windows 10/11 启用「OpenSSH 客户端」（设置 → 应用 → 可选功能）
  2. 编辑 remote.env，填写 HOST / USER，推荐配置 SSH 密钥免密码
  3. 本机安装 Python 3.8+（仅用于运行这个连接工具）

用法：
  dbterm_gui.bat                     打开图形界面（推荐）
  dbterm.bat                         进入远程交互终端
  dbterm.bat status                  在服务器执行单条命令
  dbterm.bat find roster position=班长
  dbterm.bat --check                 检查 SSH、sudo、数据库与路径
  dbterm.bat --deploy                只上传最新 dbterm.py
  dbterm.bat --no-tty <命令>         不为远程命令分配终端
  dbterm.bat --config <文件>         使用其他配置文件
  dbterm.bat --no-color              关闭远程终端颜色
  dbterm.bat help                    查看服务器端完整命令帮助

说明：
  - 除本帮助列出的 --check / --deploy / --no-tty / --config 外，
    其余参数原样传给服务器上的 dbterm.py。
  - 默认以 huiyingzhai 用户执行（remote.env 的 SUDO_USER），
    首次可能需要输入 sudo 密码。
  - 远程备份默认保存到 REMOTE_HOME/backups。
"""
    )


def parse_args(argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    options: dict[str, Any] = {
        "check": False,
        "deploy": False,
        "no_tty": False,
        "config": None,
        "help": False,
        "version": False,
    }
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--check":
            options["check"] = True
        elif token == "--deploy":
            options["deploy"] = True
        elif token == "--no-tty":
            options["no_tty"] = True
        elif token == "--config":
            index += 1
            if index >= len(argv):
                raise ConfigError("--config 缺少文件路径")
            options["config"] = argv[index]
        elif token.startswith("--config="):
            options["config"] = token.split("=", 1)[1]
        elif token in ("-h", "--help"):
            options["help"] = True
        elif token in ("-v", "--version"):
            options["version"] = True
        else:
            break
        index += 1
    return options, argv[index:]


def main(argv: list[str]) -> int:
    try:
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    try:
        options, rest = parse_args(argv)
    except ConfigError as exc:
        error(str(exc))
        return 2

    if options["help"]:
        print_help()
        return 0
    if options["version"]:
        print(f"dbterm-remote {VERSION}")
        return 0

    config_path = (
        Path(options["config"]).expanduser().resolve()
        if options["config"]
        else CONFIG_FILE
    )

    try:
        config = load_config(config_path)
        if options["deploy"]:
            upload_script(config, force=True)
            info(f"已上传到 {config['HOST']}:{config['REMOTE_SCRIPT']}")
            return 0

        upload_script(config)

        if options["check"]:
            info("====== 远程自检 ======")
            info(f"SSH     {config['USER']}@{config['HOST']}:{config['PORT']}")
            info(f"服务器  {config['SERVER_ROOT']}")
            info(f"执行用户 {config['SUDO_USER'] or config['USER']}")
            info("正在读取数据库状态 ...")
            info("")
            code = run_remote(config, ["status"], tty=not options["no_tty"])
            if code == 0:
                info("")
                info("自检通过：SSH、远程 Python、数据库访问均正常。")
            return code

        return run_remote(config, rest, tty=not options["no_tty"])
    except ConfigError as exc:
        error(str(exc))
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
