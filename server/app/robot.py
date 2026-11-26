# 企业微信群机器人通知（可选）
# 在 .env 中填写 WECHAT_ROBOT_WEBHOOK 后生效；留空时所有函数直接返回，不影响业务。
#
# 获取方式：企业微信群 → 右上角「…」→ 群机器人 → 添加机器人 → 复制 Webhook 地址。
# 注意：Webhook 等同于群内发消息权限，只能写在服务器 .env（600），不要提交到仓库。
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx

from .config import config

MAX_TEXT = 600


def _fmt_time(ms: Any) -> str:
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    try:
        return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def _clip(value: Any, limit: int = MAX_TEXT) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _web_link() -> str:
    base = (config.base_url or "").rstrip("/")
    if not base:
        return ""
    return "> 网页查看：[%s/huiyingzhai/](%s/huiyingzhai/)" % (base, base)


async def send_markdown(title: str, lines: list[str]) -> None:
    """向企业微信群机器人发送 Markdown 消息；未配置或失败只打日志。"""
    webhook = config.wechat_robot_webhook
    if not webhook:
        return
    content = "## " + _clip(title, 80) + "\n" + "\n".join(line for line in lines if line)
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(webhook, json=payload)
            if resp.status_code >= 300:
                print("微信群机器人通知失败：HTTP %s %s" % (resp.status_code, resp.text[:200]))
                return
            data = resp.json()
            if data.get("errcode") not in (None, 0):
                print("微信群机器人通知失败：%s" % data)
    except Exception as exc:  # 通知失败绝不能影响发布
        print("微信群机器人通知失败：", exc)


def _send_later(title: str, lines: list[str]) -> None:
    if not config.wechat_robot_webhook:
        return
    try:
        asyncio.get_running_loop().create_task(send_markdown(title, lines))
    except RuntimeError:
        print("微信群机器人通知跳过：没有运行中的事件循环")


def notify_homework(*, title: Any, desc: Any, deadline: Any, creator_name: Any) -> None:
    lines = [
        "> 类型：**班级作业**",
        "> 标题：**%s**" % _clip(title, 100),
        "> 发布：%s" % _clip(creator_name, 40),
        "> 截止：%s" % (_fmt_time(deadline) or "未设置"),
    ]
    if desc:
        lines.append("> 说明：%s" % _clip(desc, 200))
    link = _web_link()
    if link:
        lines.append(link)
    _send_later("新作业提醒", lines)


def notify_activity(
    *, title: Any, place: Any, activity_time: Any, content: Any, creator_name: Any
) -> None:
    lines = [
        "> 类型：**班级活动**",
        "> 标题：**%s**" % _clip(title, 100),
        "> 时间：%s" % (_fmt_time(activity_time) or "待定"),
        "> 地点：%s" % _clip(place or "待定", 80),
        "> 发布：%s" % _clip(creator_name, 40),
    ]
    if content:
        lines.append("> 说明：%s" % _clip(content, 200))
    link = _web_link()
    if link:
        lines.append(link)
    _send_later("新活动提醒", lines)


def notify_subject_task(
    *, subject_name: Any, title: Any, content: Any, deadline: Any, creator_name: Any
) -> None:
    lines = [
        "> 类型：**学科任务 · %s**" % _clip(subject_name, 40),
        "> 标题：**%s**" % _clip(title, 100),
        "> 发布：%s" % _clip(creator_name, 40),
        "> 截止：%s" % (_fmt_time(deadline) or "未设置"),
    ]
    if content:
        lines.append("> 说明：%s" % _clip(content, 200))
    link = _web_link()
    if link:
        lines.append(link)
    _send_later("新任务通知", lines)
