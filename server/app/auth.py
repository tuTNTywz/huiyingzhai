# 微信登录：wx.login 的 code 换取 openid，签发并校验自建会话 token
from __future__ import annotations

from typing import Optional

import httpx

from . import db
from .config import config


# code 换 openid（开发模式不请求微信服务器）
async def code2session(code: str) -> dict:
    if config.dev_mode:
        return {"openid": config.dev_openid, "sessionKey": "", "dev": True}
    if not config.appid or not config.secret:
        raise RuntimeError("服务端未配置微信凭据（WX_APPID / WX_SECRET）")
    if not code:
        raise RuntimeError("缺少微信登录 code")

    url = f"{config.wx_api_base}/sns/jscode2session"
    params = {
        "appid": config.appid,
        "secret": config.secret,
        "js_code": code,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            data = resp.json()
    except Exception as exc:  # 网络异常 / 非 JSON 响应
        raise RuntimeError("微信登录服务不可用，请稍后重试") from exc

    if not data.get("openid"):
        print("jscode2session 失败", data)
        raise RuntimeError(data.get("errmsg") or "微信登录失败，请稍后重试")
    return {"openid": data["openid"], "sessionKey": data.get("session_key", "")}


# 从 Authorization: Bearer <token> 中解析登录用户
def resolve_user(token: str) -> tuple[Optional[str], Optional[dict]]:
    openid = db.get_session_openid(token)
    if not openid:
        return None, None
    user = db.find_one("users", lambda u: u.get("_openid") == openid)
    return openid, user
