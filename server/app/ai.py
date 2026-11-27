# AI 智能填充：用户自带 API Key，服务端加密保存并代理模型请求
from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

import httpx
from nacl.secret import SecretBox
from nacl.utils import random as nacl_random

from .config import config

# 只允许列表内的 OpenAI 兼容服务，避免用户传入任意 URL 造成 SSRF
PROVIDERS = {
    "deepseek": {
        "base": "https://api.deepseek.com/v1",
        "models": {"deepseek-chat", "deepseek-reasoner"},
    },
    "qwen": {
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": {"qwen-plus", "qwen-turbo", "qwen-max"},
    },
    "moonshot": {
        "base": "https://api.moonshot.cn/v1",
        "models": {"moonshot-v1-8k", "moonshot-v1-32k"},
    },
    "zhipu": {
        "base": "https://open.bigmodel.cn/api/paas/v4",
        "models": {"glm-4-flash", "glm-4-plus"},
    },
    "openai": {
        "base": "https://api.openai.com/v1",
        "models": {"gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"},
    },
    "siliconflow": {
        "base": "https://api.siliconflow.cn/v1",
        "models": {"Qwen/Qwen2.5-7B-Instruct"},
    },
}

MODEL_LABELS = {
    "deepseek-chat": "DeepSeek Chat",
    "deepseek-reasoner": "DeepSeek Reasoner",
    "qwen-plus": "通义千问 Plus",
    "qwen-turbo": "通义千问 Turbo",
    "qwen-max": "通义千问 Max",
    "moonshot-v1-8k": "Kimi 8K",
    "moonshot-v1-32k": "Kimi 32K",
    "glm-4-flash": "GLM-4-Flash",
    "glm-4-plus": "GLM-4-Plus",
    "gpt-4o-mini": "GPT-4o mini",
    "gpt-4o": "GPT-4o",
    "gpt-4.1-mini": "GPT-4.1 mini",
    "Qwen/Qwen2.5-7B-Instruct": "SiliconFlow Qwen2.5 7B",
}


def public_models() -> list[dict]:
    rows = []
    for provider, item in PROVIDERS.items():
        for model in sorted(item["models"]):
            rows.append(
                {
                    "provider": provider,
                    "model": model,
                    "name": MODEL_LABELS.get(model, model),
                }
            )
    return rows


def validate_model(provider: Any, model: Any) -> tuple[str, str]:
    p = str(provider or "").strip()
    m = str(model or "").strip()
    item = PROVIDERS.get(p)
    if not item or m not in item["models"]:
        raise ValueError("不支持的模型")
    return p, m


def _secret_key() -> bytes:
    secret = config.ai_key_secret or config.web_panel_secret
    if not secret:
        raise RuntimeError("未配置 AI_KEY_SECRET 或 WEB_PANEL_SECRET")
    return hashlib.sha256(("huiyingzhai-ai:" + secret).encode("utf-8")).digest()


def encrypt_key(plain: str) -> str:
    key = _secret_key()
    box = SecretBox(key)
    nonce = nacl_random(SecretBox.NONCE_SIZE)
    encrypted = box.encrypt(plain.encode("utf-8"), nonce)
    return base64.b64encode(bytes(encrypted)).decode("ascii")


def decrypt_key(token: str) -> str:
    key = _secret_key()
    raw = base64.b64decode(str(token or "").encode("ascii"))
    return SecretBox(key).decrypt(raw).decode("utf-8")


def key_hint(plain: str) -> str:
    text = str(plain or "")
    return ("****" + text[-4:]) if len(text) > 4 else "****"


def _extract_json(content: str) -> dict:
    text = str(content or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("模型返回格式不正确")
    return data


def _normalize_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    mapping = {
        "homework": "homework", "作业": "homework",
        "activity": "activity", "活动": "activity",
        "volunteer": "volunteer", "志愿活动": "volunteer", "志愿": "volunteer",
        "notice": "notice", "通知": "notice",
        "subject": "subject", "学科任务": "subject",
    }
    return mapping.get(text, "")


async def fill_fields(provider: str, model: str, api_key: str, text: str) -> dict:
    p, m = validate_model(provider, model)
    base = PROVIDERS[p]["base"].rstrip("/")
    system = (
        "你是班级发布助手。请从用户提供的文字中提取发布信息，只输出一个 JSON 对象，"
        "不要输出 Markdown、代码块或解释。字段：\n"
        "type: homework（作业）/ activity（活动）/ volunteer（志愿活动）/ notice（通知）/ subject（学科任务）之一；\n"
        "title: 标题；content: 完整正文或说明；\n"
        "deadline: 截止时间；time: 活动或志愿开始时间；end: 结束时间；"
        "时间格式统一为 YYYY-MM-DD HH:mm；\n"
        "place: 地点；subject: 学科名称（如 高等数学、大学英语）；\n"
        "organizer: 组织单位；quota: 招募人数；contact: 联系方式；signupUrl: 报名链接。\n"
        "没有信息的字段用空字符串。"
    )
    payload = {
        "model": m,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": str(text or "")[:6000]},
        ],
        "temperature": 0.1,
        "max_tokens": 800,
    }
    headers = {"Authorization": "Bearer " + api_key}
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(base + "/chat/completions", json=payload, headers=headers)
    except Exception as exc:
        raise RuntimeError("无法连接模型服务：%s" % exc) from exc

    if resp.status_code in (401, 403):
        raise RuntimeError("API Key 无效或没有该模型权限")
    if resp.status_code >= 300:
        detail = resp.text[:200]
        raise RuntimeError("模型服务返回 HTTP %s：%s" % (resp.status_code, detail))
    try:
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("模型返回格式异常") from exc

    data = _extract_json(content)
    result = {
        "type": _normalize_type(data.get("type")),
        "title": str(data.get("title") or "").strip(),
        "content": str(data.get("content") or "").strip(),
        "deadline": str(data.get("deadline") or "").strip(),
        "time": str(data.get("time") or "").strip(),
        "end": str(data.get("end") or "").strip(),
        "place": str(data.get("place") or "").strip(),
        "subject": str(data.get("subject") or "").strip(),
        "organizer": str(data.get("organizer") or "").strip(),
        "quota": str(data.get("quota") or "").strip(),
        "contact": str(data.get("contact") or "").strip(),
        "signupUrl": str(data.get("signupUrl") or "").strip(),
    }
    if not result["type"]:
        raise RuntimeError("模型没有识别出发布类型")
    return result
