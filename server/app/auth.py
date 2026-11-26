# 会话解析：网页登录签发的 token → 当前用户
from __future__ import annotations

from typing import Optional

from . import db


def resolve_user(token: str) -> tuple[Optional[str], Optional[dict]]:
    openid = db.get_session_openid(token)
    if not openid:
        return None, None
    user = db.find_one("users", lambda u: u.get("_openid") == openid)
    return openid, user
