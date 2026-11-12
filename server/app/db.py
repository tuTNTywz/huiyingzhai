# SQLite 文档存储：保持与原云开发集合一致的文档结构（_id / _openid / 毫秒时间戳）
# 数据量是班级规模，查询在内存中过滤即可；如需更大规模可在此层替换为 MongoDB。
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from functools import cmp_to_key
from typing import Any, Callable, Optional

from .config import config

config.data_dir.mkdir(parents=True, exist_ok=True)
db_file = config.data_dir / "huiyingzhai.sqlite"

_conn = sqlite3.connect(str(db_file), check_same_thread=False)
_conn.row_factory = sqlite3.Row
_lock = threading.RLock()

with _lock:
    _conn.execute("PRAGMA journal_mode = WAL")
    _conn.execute("PRAGMA synchronous = NORMAL")
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
          collection TEXT NOT NULL,
          id         TEXT NOT NULL,
          data       TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          PRIMARY KEY (collection, id)
        );
        CREATE INDEX IF NOT EXISTS idx_documents_collection ON documents(collection);
        CREATE INDEX IF NOT EXISTS idx_documents_created ON documents(collection, created_at);

        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY,
          openid     TEXT NOT NULL,
          expire_at  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_expire ON sessions(expire_at);
        """
    )
    _conn.commit()


def now_ms() -> int:
    return int(time.time() * 1000)


def _sha256(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _parse(raw: str) -> Optional[dict]:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


def all(collection: str) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT data FROM documents WHERE collection = ?", (collection,)
        ).fetchall()
    return [doc for doc in (_parse(row["data"]) for row in rows) if doc]


def _compare(a: dict, b: dict, field: str, direction: str) -> int:
    av = a.get(field)
    bv = b.get(field)
    if av == bv:
        return 0
    if av is None:
        return 1
    if bv is None:
        return -1
    try:
        less = av < bv
    except TypeError:
        less = str(av) < str(bv)
    step = 1 if direction == "asc" else -1
    return -step if less else step


# 通用查询：predicate 可选，sort_by 支持文档字段，limit 截断
def find(
    collection: str,
    predicate: Optional[Callable[[dict], bool]] = None,
    *,
    sort_by: Optional[str] = None,
    direction: str = "desc",
    limit: Optional[int] = None,
) -> list[dict]:
    rows = all(collection)
    if callable(predicate):
        rows = [row for row in rows if predicate(row)]
    if sort_by:
        rows.sort(key=cmp_to_key(lambda a, b: _compare(a, b, sort_by, direction)))
    if limit and limit > 0:
        rows = rows[:limit]
    return rows


def find_one(
    collection: str, predicate: Optional[Callable[[dict], bool]] = None
) -> Optional[dict]:
    rows = find(collection, predicate, limit=1)
    return rows[0] if rows else None


def get(collection: str, doc_id: Any) -> Optional[dict]:
    if not doc_id:
        return None
    with _lock:
        row = _conn.execute(
            "SELECT data FROM documents WHERE collection = ? AND id = ?",
            (collection, str(doc_id)),
        ).fetchone()
    return _parse(row["data"]) if row else None


def insert(collection: str, doc: Optional[dict] = None) -> dict:
    payload = dict(doc or {})
    doc_id = payload.get("_id") or str(uuid.uuid4())
    data = {**payload, "_id": doc_id}
    with _lock:
        _conn.execute(
            "INSERT INTO documents (collection, id, data, created_at) VALUES (?, ?, ?, ?)",
            (collection, doc_id, json.dumps(data, ensure_ascii=False), now_ms()),
        )
        _conn.commit()
    return data


def update(collection: str, doc_id: Any, patch: dict) -> Optional[dict]:
    current = get(collection, doc_id)
    if not current:
        return None
    merged = {**current, **patch, "_id": current["_id"]}
    with _lock:
        _conn.execute(
            "UPDATE documents SET data = ? WHERE collection = ? AND id = ?",
            (json.dumps(merged, ensure_ascii=False), collection, current["_id"]),
        )
        _conn.commit()
    return merged


def remove(collection: str, doc_id: Any) -> bool:
    if not doc_id:
        return False
    with _lock:
        cursor = _conn.execute(
            "DELETE FROM documents WHERE collection = ? AND id = ?",
            (collection, str(doc_id)),
        )
        _conn.commit()
    return cursor.rowcount > 0


def remove_where(collection: str, predicate: Callable[[dict], bool]) -> int:
    targets = [doc["_id"] for doc in find(collection, predicate)]
    with _lock:
        for doc_id in targets:
            _conn.execute(
                "DELETE FROM documents WHERE collection = ? AND id = ?",
                (collection, doc_id),
            )
        _conn.commit()
    return len(targets)


def count(collection: str, predicate: Optional[Callable[[dict], bool]] = None) -> int:
    return len(find(collection, predicate))


# ===== 登录会话 =====
def create_session(openid: str) -> str:
    token = secrets.token_hex(32)
    expire_at = now_ms() + config.session_days * 24 * 60 * 60 * 1000
    with _lock:
        _conn.execute(
            "INSERT INTO sessions (token_hash, openid, expire_at) VALUES (?, ?, ?)",
            (_sha256(token), openid, expire_at),
        )
        _conn.commit()
    return token


def get_session_openid(token: Any) -> Optional[str]:
    if not token:
        return None
    token_hash = _sha256(token)
    with _lock:
        row = _conn.execute(
            "SELECT token_hash, openid, expire_at FROM sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if not row:
            return None
        if row["expire_at"] < now_ms():
            _conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            _conn.commit()
            return None
    return row["openid"]


def cleanup_sessions() -> None:
    with _lock:
        _conn.execute("DELETE FROM sessions WHERE expire_at < ?", (now_ms(),))
        _conn.commit()
