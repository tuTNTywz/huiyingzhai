# 服务端配置：全部来自 server/.env（参考 .env.example）
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")



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
        # 对外访问地址（反向代理域名），无末尾斜杠
        self.base_url = _str("BASE_URL").rstrip("/")

        # 登录有效期（天）
        self.session_days = _int("SESSION_DAYS", 30)

        # 数据与附件目录，默认放在 server/data、server/uploads
        data_dir = _str("DATA_DIR")
        self.data_dir = Path(data_dir).resolve() if data_dir else ROOT / "data"
        upload_dir = _str("UPLOAD_DIR")
        self.upload_dir = Path(upload_dir).resolve() if upload_dir else ROOT / "uploads"

        self.max_upload_mb = _int("MAX_UPLOAD_MB", 20)

        # 网页面板与后端之间的内部共享密钥（install.sh 生成）
        self.web_panel_secret = _str("WEB_PANEL_SECRET")
        # 用户 AI API Key 的加密密钥；留空时回退到 WEB_PANEL_SECRET
        self.ai_key_secret = _str("AI_KEY_SECRET")

        # 可选：企业微信群机器人 Webhook。留空则不发企业微信通知
        self.wechat_robot_webhook = _str("WECHAT_ROBOT_WEBHOOK")
        # 可选：PushPlus 群组推送（不装企业微信的方案）
        self.pushplus_token = _str("PUSHPLUS_TOKEN")
        self.pushplus_topic = _str("PUSHPLUS_TOPIC")
        # 可选：PushPlus QQ 群配置编码（第三方，备用）
        self.pushplus_qq_option = _str("PUSHPLUS_QQ_OPTION")

        # 可选：QQ 官方机器人（QQ 开放平台）
        self.qq_app_id = _str("QQ_APP_ID")
        self.qq_app_secret = _str("QQ_APP_SECRET")
        self.qq_bot_secret = _str("QQ_BOT_SECRET")
        self.qq_group_openid = _str("QQ_GROUP_OPENID")


config = Config()
