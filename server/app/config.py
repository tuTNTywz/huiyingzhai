# 服务端配置：全部来自 server/.env（参考 .env.example）
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


def _bool(value, fallback: bool = False) -> bool:
    if value is None or value == "":
        return fallback
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


def _str(name: str, default: str = "") -> str:
    return str(os.environ.get(name) or default).strip()


class Config:
    def __init__(self) -> None:
        self.root = ROOT

        # 服务端口（Nginx 反向代理到这里）
        self.port = _int("PORT", 3000)
        # 对外访问地址（小程序服务器域名 / 反向代理域名），无末尾斜杠
        self.base_url = _str("BASE_URL").rstrip("/")

        # 微信小程序凭据：在微信公众平台「开发管理 - 开发设置」获取
        self.appid = _str("WX_APPID")
        self.secret = _str("WX_SECRET")
        # 开发模式需显式开启：不发真实请求，所有登录固定为 DEV_OPENID
        # 生产环境必须保持关闭，避免所有用户被识别成同一账号
        self.dev_mode = _bool(os.environ.get("DEV_MODE"), False)
        self.dev_openid = _str("DEV_OPENID", "dev-openid-1") or "dev-openid-1"

        # 登录有效期（天）
        self.session_days = _int("SESSION_DAYS", 30)

        # 订阅消息模板 ID 与推送版本：developer / trial / formal
        self.homework_tmpl_id = _str(
            "WX_TMPL_HOMEWORK", "6avlHCT0IR-fd1hk3c15hQDn0X0VWmuQs0rcdbN5FgM"
        )
        self.push_state = _str("WX_PUSH_STATE", "trial") or "trial"

        # 数据与附件目录，默认放在 server/data、server/uploads
        data_dir = _str("DATA_DIR")
        self.data_dir = Path(data_dir).resolve() if data_dir else ROOT / "data"
        upload_dir = _str("UPLOAD_DIR")
        self.upload_dir = Path(upload_dir).resolve() if upload_dir else ROOT / "uploads"

        self.max_upload_mb = _int("MAX_UPLOAD_MB", 20)

        # 微信接口地址（测试时可替换为 mock）
        self.wx_api_base = _str("WX_API_BASE", "https://api.weixin.qq.com").rstrip("/")


config = Config()
