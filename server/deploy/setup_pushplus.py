#!/usr/bin/env python3
"""交互式配置 PushPlus 群组通知（需要 root 运行）。

用法：
    sudo /usr/local/sbin/huiyingzhai-pushplus-setup

只向 /opt/huiyingzhai/server/.env 写入 PUSHPLUS_TOKEN / PUSHPLUS_TOPIC /
PUSHPLUS_QQ_OPTION，不会把密钥打印到屏幕或日志。写入前自动备份 .env。
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


def post(payload: dict) -> tuple[bool, str]:
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


def send_test(token: str, topic: str) -> tuple[bool, str]:
    return post(
        {
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
    )


def send_test_qq(token: str, option: str) -> tuple[bool, str]:
    return post(
        {
            "token": token,
            "channel": "qq",
            "option": option,
            "title": "汇英斋 QQ 通知测试",
            "content": "## 配置成功\n> 以后发布作业、活动、学科任务时，QQ 群会收到通知。",
            "template": "markdown",
        }
    )


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
    topic = ask("微信群组编码 Topic: ")
    qq_option = ask("QQ 群配置编码 option（可选，直接回车跳过）: ")
    if not token or not topic:
        fail("Token 和微信群组编码都不能为空")
    if any(ch.isspace() for ch in token + topic + qq_option):
        fail("Token / 编码中不能有空格或换行")

    original = ENV_PATH.read_text(encoding="utf-8")
    updated = upsert(upsert(original, "PUSHPLUS_TOKEN", token), "PUSHPLUS_TOPIC", topic)
    if qq_option:
        updated = upsert(updated, "PUSHPLUS_QQ_OPTION", qq_option)

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
        print("微信测试消息发送失败：%s" % detail)
        print("请检查 Token、群组编码是否正确；配置已保存，可修正后重跑本脚本。")

    if qq_option:
        print("正在发送 QQ 群测试消息…")
        ok_qq, detail_qq = send_test_qq(token, qq_option)
        if ok_qq:
            print("QQ 群已接受测试消息（%s）。" % detail_qq)
        else:
            print("QQ 群测试消息发送失败：%s" % detail_qq)
            print("确认机器人已进群、群主已允许主动消息、option 编码正确。")

    print("正在重启 %s…" % SERVICE)
    result = subprocess.run(["systemctl", "restart", SERVICE])
    if result.returncode != 0:
        fail("服务重启失败，请执行 systemctl status %s 查看" % SERVICE)
    print("完成。之后发布作业/活动/学科任务会自动推送到群组。")


if __name__ == "__main__":
    main()
