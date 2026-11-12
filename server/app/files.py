# 附件上传与删除：文件保存在本地 uploads 目录，由 Nginx 直接对外提供
from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import UploadFile

from .config import config

config.upload_dir.mkdir(parents=True, exist_ok=True)


class UploadTooLarge(Exception):
    pass


# 目录只允许字母数字、下划线、中划线与斜杠，杜绝路径穿越
def safe_dir(raw: Any) -> str:
    cleaned = re.sub(
        r"[^a-zA-Z0-9_\-/]", "", str(raw or "misc").replace("\\", "/")
    )
    parts = [part for part in cleaned.split("/") if part and part not in (".", "..")]
    cleaned = re.sub(r"/{2,}", "/", "/".join(parts))[:120]
    return cleaned or "misc"


# 部分客户端把 UTF-8 文件名按 latin1 传递，这里还原为中文
def fix_original_name(raw: Any) -> str:
    value = str(raw or "")
    if not value or any(ord(ch) > 0xFF for ch in value):
        return value
    try:
        decoded = value.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    return value if "�" in decoded else decoded


# 保留中文与常见文件名字符，去掉路径分隔符
def safe_name(raw: Any) -> str:
    base = Path(str(raw or "file").replace("\\", "/")).name
    cleaned = re.sub(r"[^\w.\-]", "_", base)[-120:]
    return cleaned or "file"


async def save_upload(upload: Optional[UploadFile], raw_dir: Any) -> dict:
    if upload is None or not upload.filename:
        raise ValueError("未收到文件")

    directory = safe_dir(raw_dir)
    target_dir = config.upload_dir / directory
    target_dir.mkdir(parents=True, exist_ok=True)

    original_name = fix_original_name(upload.filename)
    name = safe_name(original_name)
    filename = f"{int(time.time() * 1000)}_{secrets.token_hex(6)}_{name}"
    dest = target_dir / filename
    max_bytes = config.max_upload_mb * 1024 * 1024
    total = 0

    try:
        with dest.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise UploadTooLarge()
                handle.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise

    content_type = str(upload.content_type or "")
    return {
        "fileID": f"/uploads/{directory}/{filename}",
        "name": original_name or name,
        "size": total,
        "type": "image" if content_type.startswith("image/") else "file",
    }


# 仅允许删除 uploads 目录内的文件
def resolve_upload_path(file_id: Any) -> Optional[Path]:
    value = str(file_id or "")
    prefix = "/uploads/"
    if not value.startswith(prefix):
        return None
    root = config.upload_dir.resolve()
    try:
        full = (root / value[len(prefix):]).resolve()
    except (OSError, ValueError):
        return None
    if full != root and root not in full.parents:
        return None
    return full


async def delete_uploads(file_list: Any) -> None:
    items = file_list if isinstance(file_list, list) else []
    for file_id in items:
        full = resolve_upload_path(file_id)
        if not full:
            continue
        try:
            full.unlink(missing_ok=True)
        except OSError as exc:
            print("删除附件失败", full, exc)
