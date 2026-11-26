#!/usr/bin/env python3
"""交互式配置 PushPlus 群组通知（需要 root 运行）。

用法：
    sudo /usr/local/sbin/huiyingzhai-pushplus-setup

只向 /opt/huiyingzhai/server/.env 写入 PUSHPLUS_TOKEN / PUSHPLUS_TOPIC，
不会把密钥打印到屏幕或日志。写入前自动备份 .env。
"""
from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path("/opt/huiyingzhai/server/.env")
BACKUP_DIR = Path("/opt/huiyingzhai/backups")
SERVICE = "huiyingzhai"
API = "https://www.pushplus.plus/send"


def fail(message: str) -> None:
    print("错误：" + message)
    sys.exit(1)


def ask(label: str, secret: bool = False) -> str:
    try:
        value = getpass.getpass(label) if secret else input(label)
    except (EOFError, KeyboardInterrupt):
        print()
        fail("已取消")
    return value.strip()


def upsert(text: str, key: str, value: str) -> str:
    pattern = re.compile(r"^%s=.*$" % re.escape(key), re.MULTILINE)
    line = "%s=%s" % (key, value)
    if pattern.search(text):
        return pattern.sub(line, text)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


def send_test(token: str, topic: str) -> tuple[bool, str]:
    payload = {
        "token": token,
        "topic": topic,
        "title": "汇英斋通知测试",
        "content": (
            "## 配置成功\n"
            "> 这是一条测试消息。\n"
            "> 以后发布作业、活动、学科任务时，群组成员会收到通知。"
        ),
        "template": "markdown",
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        API, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return False, "HTTP %s" % exc.code
    except Exception as exc:
        return False, str(exc)
    if body.get("code") == 200:
        return True, "code=200"
    return False, "code=%s msg=%s" % (body.get("code"), body.get("msg"))


def main() -> None:
    if os.geteuid() != 0:
        fail("请使用 sudo 运行")
    if not ENV_PATH.exists():
        fail("找不到 %s" % ENV_PATH)

    print("PushPlus 群组通知配置")
    print("1) 用微信登录 https://www.pushplus.plus/ ，控制台首页复制「Token」")
    print("2) 发送消息 → 一对多消息 → 新建群组，记下「群组编码」")
    print("Token 和群组编码只保存在服务器 .env，不会显示或上传。\n")

    token = ask("PushPlus Token（输入时不显示）: ", secret=True)
    topic = ask("群组编码 Topic: ")
    if not token or not topic:
        fail("Token 和群组编码都不能为空")
    if any(ch.isspace() for ch in token + topic):
        fail("Token / 群组编码中不能有空格或换行")

    original = ENV_PATH.read_text(encoding="utf-8")
    updated = upsert(upsert(original, "PUSHPLUS_TOKEN", token), "PUSHPLUS_TOPIC", topic)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / ("env-before-pushplus-%s" % time.strftime("%Y%m%d-%H%M%S"))
    backup.write_text(original, encoding="utf-8")
    os.chmod(backup, 0o600)

    tmp = ENV_PATH.with_name(ENV_PATH.name + ".tmp")
    tmp.write_text(updated, encoding="utf-8")
    os.chmod(tmp, 0o600)
    try:
        import grp
        import pwd

        uid = pwd.getpwnam("huiyingzhai").pw_uid
        gid = grp.getgrnam("huiyingzhai").gr_gid
        os.chown(tmp, uid, gid)
    except (KeyError, ImportError):
        pass
    os.replace(tmp, ENV_PATH)
    print("\n已写入 %s（备份：%s）" % (ENV_PATH, backup))

    print("正在发送测试消息…")
    ok, detail = send_test(token, topic)
    if ok:
        print("PushPlus 已接受测试消息（%s）。" % detail)
        print("如果群组还没有成员，先分享群组二维码给同学扫码加入。")
    else:
        print("测试消息发送失败：%s" % detail)
        print("请检查 Token、群组编码是否正确；配置已保存，可修正后重跑本脚本。")

    print("正在重启 %s…" % SERVICE)
    result = subprocess.run(["systemctl", "restart", SERVICE])
    if result.returncode != 0:
        fail("服务重启失败，请执行 systemctl status %s 查看" % SERVICE)
    print("完成。之后发布作业/活动/学科任务会自动推送到群组。")


if __name__ == "__main__":
    main()
