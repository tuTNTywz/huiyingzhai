# 微信订阅消息推送：access_token 缓存 + 逐个用户发送、扣减订阅次数
from __future__ import annotations

import asyncio
import time
from datetime import datetime

import httpx

from . import db
from .config import config

_token_cache = {"token": "", "expire_at": 0}


def _now_ms() -> int:
    return int(time.time() * 1000)


async def get_access_token() -> str:
    now = _now_ms()
    if _token_cache["token"] and now < _token_cache["expire_at"] - 60 * 1000:
        return _token_cache["token"]
    if not config.appid or not config.secret:
        raise RuntimeError("未配置 WX_APPID / WX_SECRET")

    url = f"{config.wx_api_base}/cgi-bin/token"
    params = {
        "grant_type": "client_credential",
        "appid": config.appid,
        "secret": config.secret,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
        data = resp.json()
    if not data.get("access_token"):
        raise RuntimeError(
            f"获取 access_token 失败：{data.get('errcode') or ''} {data.get('errmsg') or ''}".strip()
        )
    _token_cache["token"] = data["access_token"]
    _token_cache["expire_at"] = now + (int(data.get("expires_in") or 7200)) * 1000
    return _token_cache["token"]


# 订阅消息要求的日期格式：yyyy-MM-dd HH:mm
def notice_time_text(ts) -> str:
    try:
        value = int(ts)
    except (TypeError, ValueError):
        value = 0
    seconds = (value or _now_ms()) / 1000
    return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")


async def send_one(
    client: httpx.AsyncClient, token: str, payload: dict, subscription: dict
) -> None:
    resp = await client.post(
        f"{config.wx_api_base}/cgi-bin/message/subscribe/send",
        params={"access_token": token},
        json=payload,
    )
    result = resp.json()
    if result.get("errcode") == 0:
        remain = max(0, int(subscription.get("remain") or 1) - 1)
        db.update(
            "subscriptions",
            subscription["_id"],
            {"remain": remain, "updateTime": _now_ms()},
        )
        return

    print(
        "订阅消息推送失败",
        subscription.get("_openid"),
        result.get("errcode"),
        result.get("errmsg"),
    )
    # 43101：用户未订阅 / 次数已用完；40003：openid 无效。清零避免持续重试
    if result.get("errcode") in (43101, 40003):
        db.update(
            "subscriptions",
            subscription["_id"],
            {"remain": 0, "updateTime": _now_ms()},
        )


# 模板字段：thing1=标题，time4=截止时间，thing5=备注（作业与学科任务共用）
async def push_subscribe_message(
    *,
    page: str,
    title: str,
    time,
    remark: str,
    exclude_openid: str = "",
) -> None:
    if not config.homework_tmpl_id or not config.appid or not config.secret:
        return
    try:
        subscriptions = db.find(
            "subscriptions", lambda s: int(s.get("remain") or 0) > 0, limit=1000
        )
        targets = [
            s
            for s in subscriptions
            if s.get("_openid") and s.get("_openid") != exclude_openid
        ]
        if not targets:
            return

        try:
            token = await get_access_token()
        except Exception as exc:
            print("推送流程异常：", exc)
            return

        payload = {
            "template_id": config.homework_tmpl_id,
            "page": page,
            "miniprogram_state": config.push_state,
            "lang": "zh_CN",
            "data": {
                "thing1": {"value": str(title or "新通知")[:20]},
                "time4": {"value": notice_time_text(time)},
                "thing5": {
                    "value": (str(remark or "").strip()[:20] or "点击查看详情")
                },
            },
        }

        async with httpx.AsyncClient(timeout=10) as client:
            await asyncio.gather(
                *[
                    send_one(
                        client,
                        token,
                        {**payload, "touser": subscription["_openid"]},
                        subscription,
                    )
                    for subscription in targets
                ],
                return_exceptions=True,
            )
    except Exception as exc:
        print("推送流程异常", exc)
