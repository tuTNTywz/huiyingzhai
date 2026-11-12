# 汇英斋自建后端入口：FastAPI + SQLite
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, db as store, files
from .api import handle_action
from .config import config

INLINE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.upload_dir.mkdir(parents=True, exist_ok=True)
    store.cleanup_sessions()
    print(f"汇英斋服务已启动：http://127.0.0.1:{config.port}")
    print(f"对外地址：{config.base_url or '未配置（请填写 BASE_URL）'}")
    print(f"数据目录：{config.data_dir}")
    print(f"附件目录：{config.upload_dir}")
    if config.dev_mode:
        print(
            f"当前为开发模式：登录固定使用 openid={config.dev_openid}（不会请求微信服务器）"
        )
    yield


app = FastAPI(
    title="汇英斋自建后端",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _bearer_token(request: Request) -> str:
    header = str(request.headers.get("authorization") or "")
    return header[7:].strip() if header.startswith("Bearer ") else ""


def _require_user(request: Request):
    openid, user = auth.resolve_user(_bearer_token(request))
    if not openid:
        return None, JSONResponse(
            {"ok": False, "msg": "登录已过期，请重新进入小程序"}, status_code=401
        )
    if not user:
        return None, JSONResponse(
            {"ok": False, "msg": "账号档案不存在，请重新登录"}, status_code=401
        )
    return user, None


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "time": store.now_ms(),
        "baseUrl": config.base_url or None,
        "devMode": config.dev_mode,
        "push": bool(config.homework_tmpl_id and config.appid and config.secret),
    }


# 微信登录：code 换 openid，首次登录自动建档
@app.post("/api/login")
async def login(request: Request):
    body = await _json_body(request)
    try:
        result = await auth.code2session(str(body.get("code") or ""))
        openid = result["openid"]
        user = store.find_one("users", lambda u: u.get("_openid") == openid)
        if not user:
            user = store.insert(
                "users",
                {
                    "_openid": openid,
                    "nickname": "同窗",
                    "role": "student",
                    "registered": False,
                    "createTime": store.now_ms(),
                },
            )
        store.cleanup_sessions()
        token = store.create_session(openid)
        return {
            "ok": True,
            "token": token,
            "user": user,
            "registered": bool(user.get("registered")),
        }
    except Exception as exc:
        print("登录失败", exc)
        return {"ok": False, "msg": str(exc) or "登录失败，请稍后重试"}


# 业务接口：与小程序端约定 { action, data }
@app.post("/api/action")
async def action(request: Request):
    user, error = _require_user(request)
    if error:
        return error

    body = await _json_body(request)
    action_name = body.get("action")
    if not action_name:
        return {"ok": False, "msg": "缺少 action"}
    try:
        return await handle_action(user, str(action_name), body.get("data") or {})
    except Exception as exc:
        print("api error", action_name, exc)
        return {"ok": False, "msg": str(exc) or "服务异常，请稍后重试"}


# 附件上传：multipart 字段 file，可选 dir
@app.post("/api/upload")
async def upload(
    request: Request, file: Optional[UploadFile] = File(None), dir: str = Form("misc")
):
    _user, error = _require_user(request)
    if error:
        return error
    try:
        result = await files.save_upload(file, dir)
        return {"ok": True, **result}
    except files.UploadTooLarge:
        return {"ok": False, "msg": f"文件超过 {config.max_upload_mb}MB 限制"}
    except ValueError as exc:
        return {"ok": False, "msg": str(exc) or "附件上传失败"}
    except Exception as exc:
        print("保存附件失败", exc)
        return {"ok": False, "msg": str(exc) or "附件上传失败"}


@app.post("/api/deleteFiles")
async def delete_files(request: Request):
    _user, error = _require_user(request)
    if error:
        return error
    try:
        body = await _json_body(request)
        await files.delete_uploads(body.get("fileList"))
        return {"ok": True}
    except Exception:
        return {"ok": False, "msg": "附件删除失败"}


# 附件静态目录：图片可直接预览，其它类型强制下载，避免被当作网页执行
@app.get("/uploads/{full_path:path}")
async def serve_upload(full_path: str):
    parts = [part for part in full_path.split("/") if part]
    if not parts or any(part.startswith(".") for part in parts):
        return JSONResponse({"ok": False, "msg": "接口不存在"}, status_code=404)

    target = files.resolve_upload_path("/uploads/" + full_path)
    if not target or not target.is_file():
        return JSONResponse({"ok": False, "msg": "接口不存在"}, status_code=404)

    headers = {
        "Cache-Control": "public, max-age=604800",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if target.suffix.lower() not in INLINE_EXTS:
        headers["Content-Disposition"] = "attachment"
    return FileResponse(target, headers=headers)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return JSONResponse({"ok": False, "msg": "接口不存在"}, status_code=404)
    return JSONResponse(
        {"ok": False, "msg": str(exc.detail)}, status_code=exc.status_code
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception):
    print("未处理异常", exc)
    return JSONResponse(
        {"ok": False, "msg": "服务异常，请稍后重试"}, status_code=500
    )
