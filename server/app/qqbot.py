# QQ 官方机器人（QQ 开放平台）接入
#
# 只用腾讯官方接口，不经过任何第三方：
#   - Webhook 接收事件（Ed25519 验签），自动记录机器人所在群的 group_openid
#   - 发布作业/活动/学科任务时，调用官方群消息接口主动推送
#
# .env:
#   QQ_APP_ID=机器人 AppID
#   QQ_APP_SECRET=机器人 AppSecret（用于换取 access_token）
#   QQ_BOT_SECRET=机器人 Bot Secret（用于 Webhook 验签，可能和 AppSecret 相同）
#   QQ_GROUP_OPENID=（可选）群 openid；不填时由入群/群消息事件自动记录
#
# 注意：官方机器人的“主动消息”默认按群关闭，需要群主在手机 QQ 群设置中开启；
# 未开启时接口返回 40034105，本模块只记录日志，不影响其他通知渠道。
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from .config import config

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

_token_lock = asyncio.Lock()
_token_value = ""
_token_expires = 0.0


def _seed(secret: str) -> bytes:
    raw = (secret or "").encode("utf-8")
    if not raw:
        raise ValueError("QQ_BOT_SECRET 未配置")
    return (raw * (32 // len(raw) + 1))[:32]


def validation_response(plain_token: str, event_ts: str) -> dict:
    """回调地址验证（op=13）：对 event_ts + plain_token 签名。"""
    key = SigningKey(_seed(config.qq_bot_secret))
    signature = key.sign((str(event_ts) + str(plain_token)).encode("utf-8")).signature
    return {"plain_token": plain_token, "signature": signature.hex()}


def verify_signature(timestamp: str, body: bytes, signature_hex: str) -> bool:
    """普通事件验签：timestamp + body，Ed25519。"""
    if not config.qq_bot_secret or not timestamp or not signature_hex:
        return False
    try:
        signature = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    if len(signature) != 64 or signature[63] & 224:
        return False
    try:
        key = VerifyKey(bytes(SigningKey(_seed(config.qq_bot_secret)).verify_key))
        key.verify(timestamp.encode("utf-8") + body, signature)
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False


def _group_file() -> Path:
    return config.data_dir / "qq_group.json"


def _read_group() -> dict:
    try:
        data = json.loads(_group_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def group_openid() -> str:
    if config.qq_group_openid:
        return config.qq_group_openid
    data = _read_group()
    if data.get("removed"):
        return ""
    return str(data.get("group_openid") or "")


def save_group(openid: str, removed: bool = False) -> None:
    openid = str(openid or "").strip()
    if not openid:
        return
    path = _group_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        payload = {
            "group_openid": openid,
            "removed": bool(removed),
            "updateTime": int(time.time() * 1000),
        }
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        print("QQ 群信息保存失败：", exc)


def handle_event(payload: dict) -> None:
    """从官方事件里记录 group_openid；不记录聊天内容。"""
    event = str(payload.get("t") or "")
    data = payload.get("d") if isinstance(payload.get("d"), dict) else {}
    gid = str(data.get("group_openid") or "").strip()
    if not gid:
        return
    if event == "GROUP_DEL_ROBOT":
        if gid == group_openid():
            save_group(gid, removed=True)
            print("QQ 机器人已被移出群：%s" % gid)
        return
    save_group(gid)
    print("QQ 群 openid 已记录：%s（事件 %s）" % (gid, event or "unknown"))


def enabled() -> bool:
    return bool(config.qq_app_id and config.qq_app_secret and group_openid())


async def _access_token(force: bool = False) -> str:
    global _token_value, _token_expires
    async with _token_lock:
        now = time.time()
        if not force and _token_value and now < _token_expires:
            return _token_value
        payload = {"appId": config.qq_app_id, "clientSecret": config.qq_app_secret}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(TOKEN_URL, json=payload)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        token = str(data.get("access_token") or "")
        if resp.status_code != 200 or not token:
            raise RuntimeError("获取 access_token 失败：HTTP %s %s" % (resp.status_code, str(data)[:200]))
        try:
            expires = int(data.get("expires_in") or 7200)
        except (TypeError, ValueError):
            expires = 7200
        _token_value = token
        _token_expires = now + max(60, expires - 60)
        return token


def _error_hint(code: Any) -> str:
    return {
        40034101: "机器人不在群内，请先把机器人拉进群",
        40034105: "无主动消息权限，请群主在手机 QQ 群设置中开启机器人主动消息",
        40054002: "机器人被禁言",
        40054003: "机器人不是群成员",
        40054007: "消息过长",
        40054010: "消息包含不允许的链接",
    }.get(code, "")


async def send_group_text(text: str) -> tuple[bool, str]:
    if not enabled():
        return False, "QQ 官方机器人未配置（缺少 AppID/AppSecret/群 openid）"
    gid = group_openid()
    payload = {"content": str(text or "")[:1800], "msg_type": 0}
    for attempt in (0, 1):
        try:
            token = await _access_token(force=attempt == 1)
        except Exception as exc:
            return False, str(exc)
        headers = {"Authorization": "QQBot %s" % token}
        url = "%s/v2/groups/%s/messages" % (API_BASE, gid)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except Exception as exc:
            return False, "请求 QQ API 失败：%s" % exc
        if resp.status_code == 200:
            return True, "ok"
        if resp.status_code == 401 and attempt == 0:
            continue
        try:
            data = resp.json()
        except ValueError:
            data = {}
        code = data.get("code")
        hint = _error_hint(code)
        detail = "HTTP %s code=%s msg=%s" % (
            resp.status_code,
            code,
            data.get("message") or data.get("msg") or resp.text[:160],
        )
        if hint:
            detail += "（%s）" % hint
        return False, detail
    return False, "QQ API 鉴权失败"
