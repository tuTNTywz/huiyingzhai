#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理部署面板账号。

账号文件默认 /var/lib/huiyingzhai-panel/accounts.json，可用 PANEL_ACCOUNTS_FILE 覆盖。
密码只保存 PBKDF2-HMAC-SHA256 哈希。

用法：
  manage_accounts.py init [--force] [用户名 ...]   # 生成随机密码，输出 用户名<TAB>密码
  manage_accounts.py set 用户名                    # 从 stdin 或交互读取新密码
  manage_accounts.py list
  manage_accounts.py delete 用户名
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path

ITERATIONS = 600_000
MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 128
PANEL_USER = os.environ.get("PANEL_USER") or "huiyingzhai-panel"
ACCOUNTS_FILE = Path(
    os.environ.get("PANEL_ACCOUNTS_FILE")
    or "/var/lib/huiyingzhai-panel/accounts.json"
)

try:
    IS_ROOT = os.geteuid() == 0
except AttributeError:  # Windows 本地测试
    IS_ROOT = False


def now_ms() -> int:
    return int(time.time() * 1000)


def make_record(password: str) -> dict:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS
    )
    return {
        "salt": salt,
        "hash": digest.hex(),
        "iterations": ITERATIONS,
        "updatedAt": now_ms(),
    }


def load_users() -> dict:
    try:
        with ACCOUNTS_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SystemExit("读取账号文件失败：%s" % exc)
    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, dict):
        raise SystemExit("账号文件格式不正确：%s" % ACCOUNTS_FILE)
    return users


def save_users(users: dict) -> None:
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNTS_FILE.with_name(ACCOUNTS_FILE.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump({"version": 1, "users": users}, fh, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)

    if IS_ROOT:
        try:
            import pwd

            entry = pwd.getpwnam(PANEL_USER)
            os.chown(tmp, entry.pw_uid, entry.pw_gid)
            os.chown(ACCOUNTS_FILE.parent, entry.pw_uid, entry.pw_gid)
            os.chmod(ACCOUNTS_FILE.parent, 0o700)
        except KeyError:
            pass

    os.replace(tmp, ACCOUNTS_FILE)


def read_new_password() -> str:
    if sys.stdin.isatty():
        password = getpass.getpass("新密码：")
        confirm = getpass.getpass("再次输入：")
        if password != confirm:
            raise SystemExit("两次输入不一致")
    else:
        password = sys.stdin.readline().rstrip("\n")
    if len(password) < MIN_PASSWORD_LEN or len(password) > MAX_PASSWORD_LEN:
        raise SystemExit("密码长度需为 %d-%d 位" % (MIN_PASSWORD_LEN, MAX_PASSWORD_LEN))
    return password


def command_init(args) -> None:
    if ACCOUNTS_FILE.exists() and not args.force:
        raise SystemExit("账号文件已存在：%s（如需重建请加 --force）" % ACCOUNTS_FILE)
    users = {} if args.force else load_users()
    created = []
    for username in args.usernames:
        username = username.strip()
        if not username:
            continue
        password = secrets.token_urlsafe(9)
        users[username] = make_record(password)
        created.append((username, password))
    if not users:
        raise SystemExit("至少需要一个用户名")
    save_users(users)
    for username, password in created:
        print("%s\t%s" % (username, password))


def command_set(args) -> None:
    users = load_users()
    password = read_new_password()
    users[args.username] = make_record(password)
    save_users(users)
    print("已更新账号：%s" % args.username)


def command_delete(args) -> None:
    users = load_users()
    if args.username not in users:
        raise SystemExit("账号不存在：%s" % args.username)
    if len(users) <= 1:
        raise SystemExit("至少保留一个账号")
    del users[args.username]
    save_users(users)
    print("已删除账号：%s" % args.username)


def command_list(_args) -> None:
    users = load_users()
    for username in sorted(users):
        print(username)


def main() -> None:
    parser = argparse.ArgumentParser(description="管理部署面板账号")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="生成随机账号并打印密码")
    p_init.add_argument("usernames", nargs="*", default=["tutnt", "zhl"])
    p_init.add_argument("--force", action="store_true", help="覆盖已有账号文件")
    p_init.set_defaults(func=command_init)

    p_set = sub.add_parser("set", help="设置密码（stdin 或交互输入）")
    p_set.add_argument("username")
    p_set.set_defaults(func=command_set)

    p_delete = sub.add_parser("delete", help="删除账号")
    p_delete.add_argument("username")
    p_delete.set_defaults(func=command_delete)

    p_list = sub.add_parser("list", help="列出账号")
    p_list.set_defaults(func=command_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
