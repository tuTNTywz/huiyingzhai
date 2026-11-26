# 班级通知机器人（可选，两种渠道任选或同时启用）
#
# 1) PushPlus 群组（推荐，不需要安装企业微信）
#    .env: PUSHPLUS_TOKEN=...  PUSHPLUS_TOPIC=...
#    同学扫码关注 PushPlus 公众号并加入群组即可收到微信消息。
#
# 2) 企业微信群机器人（需要群成员都在企业微信）
#    .env: WECHAT_ROBOT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...
#
# 未配置任何渠道时所有函数直接返回，不影响业务。Token/Webhook 只能写在服务器
# .env（600），不要提交到仓库。
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any

import httpx

from . import qqbot
from .config import config

MAX_TEXT = 600
PUSHPLUS_API = "https://www.pushplus.plus/send"
_tasks: set[asyncio.Task] = set()


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
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _clip_block(value: Any, limit: int = 1200) -> str:
    """保留换行的正文截断，用于作业/任务/活动说明。"""
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _web_link() -> str:
    base = (config.base_url or "").rstrip("/")
    if not base:
        return ""
    return "> 网页：[在汇英斋查看](%s/huiyingzhai/)" % base


def _file_url(file_id: Any) -> str:
    fid = str(file_id or "").strip()
    if not fid.startswith("/uploads/"):
        return ""
    base = (config.base_url or "").rstrip("/")
    return base + fid if base else ""


def _attachment_lines(attachments: Any) -> list[str]:
    lines = []
    for item in attachments if isinstance(attachments, list) else []:
        if not isinstance(item, dict):
            continue
        name = _clip(item.get("name") or "附件", 60)
        url = _file_url(item.get("fileID"))
        lines.append("> 附件：[%s](%s)" % (name, url) if url else "> 附件：%s" % name)
    return lines


def _content(title: str, lines: list[str]) -> str:
    return "## " + _clip(title, 80) + "\n" + "\n".join(line for line in lines if line)


def _headline(kind: str, title: Any) -> str:
    """通知栏标题：带上具体作业/活动名称，而不是只写「新作业提醒」。"""
    name = _clip(title, 40)
    return "%s：%s" % (kind, name) if name else kind + "提醒"


async def _send_wecom(title: str, lines: list[str]) -> None:
    webhook = config.wechat_robot_webhook
    if not webhook:
        return
    payload = {"msgtype": "markdown", "markdown": {"content": _content(title, lines)}}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(webhook, json=payload)
            if resp.status_code >= 300:
                print("企业微信群机器人通知失败：HTTP %s %s" % (resp.status_code, resp.text[:200]))
                return
            data = resp.json()
            if data.get("errcode") not in (None, 0):
                print("企业微信群机器人通知失败：%s" % data)
    except Exception as exc:  # 通知失败绝不能影响发布
        print("企业微信群机器人通知失败：", exc)


async def _send_pushplus(title: str, lines: list[str]) -> None:
    token = config.pushplus_token
    topic = config.pushplus_topic
    if not token or not topic:
        return
    payload = {
        "token": token,
        "topic": topic,
        "title": _clip(title, 80),
        "content": _content(title, lines),
        "template": "markdown",
    }
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(PUSHPLUS_API, json=payload)
            if resp.status_code >= 300:
                print("PushPlus 通知失败：HTTP %s %s" % (resp.status_code, resp.text[:200]))
                return
            data = resp.json()
            if data.get("code") != 200:
                print("PushPlus 通知失败：%s" % data)
    except Exception as exc:
        print("PushPlus 通知失败：", exc)


async def _send_pushplus_qq(title: str, lines: list[str]) -> None:
    token = config.pushplus_token
    option = config.pushplus_qq_option
    if not token or not option:
        return
    payload = {
        "token": token,
        "title": _clip(title, 80),
        "content": _content(title, lines),
        "channel": "qq",
        "option": option,
        "template": "markdown",
    }
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(PUSHPLUS_API, json=payload)
            if resp.status_code >= 300:
                print("QQ 机器人通知失败：HTTP %s %s" % (resp.status_code, resp.text[:200]))
                return
            data = resp.json()
            if data.get("code") != 200:
                print("QQ 机器人通知失败：%s" % data)
    except Exception as exc:
        print("QQ 机器人通知失败：", exc)


def _qq_clean(line: str, keep_links: bool) -> str:
    text = str(line or "").lstrip("> ").replace("**", "").strip()
    if not text:
        return ""
    if keep_links:
        # 已有 [文字](url) 的链接保留；其余裸网址包成 <url>，QQ 会渲染为可点击链接
        return re.sub(r"(?<![\[(<])(https?://[^\s]+)", r"<\1>", text)
    # 纯文本回退：去掉链接语法，只保留文字；去掉裸网址
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", text)
    return re.sub(r"https?://\S+", "", text).strip()


def _qq_text(title: str, lines: list[str]) -> str:
    """QQ 官方纯文本消息：不带链接，作为 Markdown 失败时的回退。"""
    kept = [t for t in (_qq_clean(line, False) for line in lines) if t and t != "网页查看："]
    return "【%s】\n%s" % (_clip(title, 80), "\n".join(kept))


def _qq_markdown(title: str, lines: list[str]) -> str:
    """QQ 官方 Markdown：支持可点击链接（2026/04 起群聊对所有机器人开放）。"""
    kept = [t for t in (_qq_clean(line, True) for line in lines) if t]
    parts = ["# " + _clip(title, 80)] + kept
    base = (config.base_url or "").rstrip("/")
    if base and not any("/huiyingzhai/" in line for line in kept):
        parts += ["", "[在汇英斋查看](%s/huiyingzhai/)" % base]
    return "\n".join(parts)


# Markdown 权限/格式类错误：回退纯文本；主动消息无权限等错误则直接记录
_QQ_MARKDOWN_FALLBACK = ("304036", "40034008", "40034009", "40034011", "40034124", "40034127", "40054010")


async def _send_qq_official(title: str, lines: list[str]) -> None:
    if not qqbot.enabled():
        return
    ok, detail = await qqbot.send_group_markdown(_qq_markdown(title, lines))
    if ok:
        return
    if any(code in detail for code in _QQ_MARKDOWN_FALLBACK):
        ok_text, text_detail = await qqbot.send_group_text(_qq_text(title, lines))
        if ok_text:
            print("QQ Markdown 发送失败，已回退纯文本：", detail)
            return
        print("QQ 官方机器人通知失败（Markdown 与纯文本均失败）：", text_detail)
        return
    print("QQ 官方机器人通知失败：", detail)


async def send_all(title: str, lines: list[str]) -> None:
    """并发发送到已配置的渠道；单渠道失败不影响其他渠道。"""
    await asyncio.gather(
        _send_wecom(title, lines),
        _send_pushplus(title, lines),
        _send_pushplus_qq(title, lines),
        _send_qq_official(title, lines),
        return_exceptions=True,
    )


def _send_later(title: str, lines: list[str]) -> None:
    has_wechat = bool(config.pushplus_token and config.pushplus_topic)
    has_pushplus_qq = bool(config.pushplus_token and config.pushplus_qq_option)
    if (
        not config.wechat_robot_webhook
        and not has_wechat
        and not has_pushplus_qq
        and not qqbot.enabled()
    ):
        return
    try:
        task = asyncio.get_running_loop().create_task(send_all(title, lines))
    except RuntimeError:
        print("班级通知跳过：没有运行中的事件循环")
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def notify_homework(
    *, title: Any, desc: Any, deadline: Any, creator_name: Any, attachments: Any = None
) -> None:
    lines = [
        "> 截止：%s" % (_fmt_time(deadline) or "未设置"),
        "> 发布：%s" % _clip(creator_name, 40),
    ]
    if desc:
        lines.append("> 说明：%s" % _clip_block(desc, 1200))
    lines.extend(_attachment_lines(attachments))
    link = _web_link()
    if link:
        lines.append(link)
    _send_later(_headline("班级作业", title), lines)


def notify_activity(
    *, title: Any, place: Any, activity_time: Any, content: Any, creator_name: Any
) -> None:
    lines = [
        "> 时间：%s" % (_fmt_time(activity_time) or "待定"),
        "> 地点：%s" % _clip(place or "待定", 80),
        "> 发布：%s" % _clip(creator_name, 40),
    ]
    if content:
        lines.append("> 说明：%s" % _clip_block(content, 1200))
    link = _web_link()
    if link:
        lines.append(link)
    _send_later(_headline("班级活动", title), lines)


def notify_subject_task(
    *,
    subject_name: Any,
    title: Any,
    content: Any,
    deadline: Any,
    creator_name: Any,
    attachments: Any = None,
) -> None:
    lines = [
        "> 发布：%s" % _clip(creator_name, 40),
        "> 截止：%s" % (_fmt_time(deadline) or "未设置"),
    ]
    if content:
        lines.append("> 说明：%s" % _clip_block(content, 1200))
    lines.extend(_attachment_lines(attachments))
    link = _web_link()
    if link:
        lines.append(link)
    _send_later(_headline("学科任务 · %s" % _clip(subject_name, 20), title), lines)
