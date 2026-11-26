#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汇英斋服务端数据库终端。

直接读写 server/data/huiyingzhai.sqlite 中的 documents 表：
每个文档是一条 JSON（_id / _openid / 毫秒时间戳），与 server/app/db.py 完全一致。
只依赖 Python 标准库，可在 Windows、macOS、Linux 上直接运行。

用法：
    python dbterm.py                      # 进入交互终端
    python dbterm.py collections          # 单次执行
    python dbterm.py --db 路径 ls users   # 全局选项写在命令前

输入 help 查看全部命令。
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

VERSION = "1.0.0"
TOOL_DIR = Path(__file__).resolve().parent
# 可选：DBTERM_HOME 指定 backups/、exports/ 的默认目录。
# Windows 远程模式会设为服务器上的持久目录；Linux 本地使用时不设置，行为不变。
WORK_DIR = Path(os.environ.get("DBTERM_HOME") or TOOL_DIR).expanduser().resolve()
REPO_ROOT = TOOL_DIR.parent
DEFAULT_SERVER_ROOT = REPO_ROOT / "server"

MISSING = object()

# ===== 终端颜色 =====
COLOR = False


def _enable_windows_ansi() -> bool:
    """在 Windows 控制台启用 ANSI 转义（Win10+）。"""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
            return True
    except Exception:
        pass
    return False


def paint(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if COLOR else text


def bold(text: str) -> str:
    return paint("1", text)


def dim(text: str) -> str:
    return paint("2", text)


def red(text: str) -> str:
    return paint("31", text)


def green(text: str) -> str:
    return paint("32", text)


def yellow(text: str) -> str:
    return paint("33", text)


def cyan(text: str) -> str:
    return paint("36", text)


def info(text: str) -> None:
    print(text)


def ok(text: str) -> None:
    print(green("✓ ") + text)


def warn(text: str) -> None:
    print(yellow("! ") + text)


def error(text: str) -> None:
    print(red("✗ ") + text, file=sys.stderr)


# ===== 配置 =====
class Settings:
    def __init__(self) -> None:
        self.server_root: Path = DEFAULT_SERVER_ROOT
        self.db_path: Optional[Path] = None
        self.upload_dir: Optional[Path] = None
        self.yes = False
        self.readonly = False
        self.json_out = False
        self.color = True


SETTINGS = Settings()
STORE: Optional["Store"] = None


class ArgError(Exception):
    """命令参数错误（直接展示给用户）。"""


def now_ms() -> int:
    return int(time.time() * 1000)


def read_env_file(server_root: Path) -> dict[str, str]:
    """轻量解析 server/.env，只取需要的键。"""
    values: dict[str, str] = {}
    env_file = server_root / ".env"
    if not env_file.exists():
        return values
    try:
        text = env_file.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def resolve_settings() -> None:
    server_root = SETTINGS.server_root
    env = read_env_file(server_root)

    if SETTINGS.db_path is None:
        data_dir = env.get("DATA_DIR")
        base = Path(data_dir).expanduser() if data_dir else server_root / "data"
        if not base.is_absolute():
            base = (server_root / base).resolve()
        SETTINGS.db_path = base / "huiyingzhai.sqlite"

    SETTINGS.db_path = Path(SETTINGS.db_path).expanduser().resolve()

    if SETTINGS.upload_dir is None:
        upload_dir = env.get("UPLOAD_DIR")
        base = Path(upload_dir).expanduser() if upload_dir else server_root / "uploads"
        if not base.is_absolute():
            base = (server_root / base).resolve()
        SETTINGS.upload_dir = base
    SETTINGS.upload_dir = Path(SETTINGS.upload_dir).expanduser().resolve()


# ===== 显示宽度 / 表格 =====
def disp_width(text: str) -> int:
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def truncate(text: str, limit: int) -> str:
    if disp_width(text) <= limit:
        return text
    result = ""
    width = 0
    for ch in text:
        char_width = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if width + char_width > limit - 1:
            break
        result += ch
        width += char_width
    return result + "…"


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - disp_width(text))


def is_time_key(key: str) -> bool:
    return key.endswith("Time") or key in ("deadline", "expire_at")


def cell_text(key: str, value: Any, limit: int = 36) -> str:
    if value is MISSING or value is None:
        text = "null" if value is None else ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)) and is_time_key(key) and value > 10**11:
        text = datetime.datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M")
    elif isinstance(value, (list, dict)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return truncate(text, limit)


def print_table(headers: list[str], rows: list[list[str]], limit: int = 36) -> None:
    if not rows:
        info(dim("（空）"))
        return
    cells: list[list[str]] = []
    for row in rows:
        cells.append([truncate(str(value), limit) for value in row])
    widths = []
    for index, header in enumerate(headers):
        width = max([disp_width(header)] + [disp_width(row[index]) for row in cells])
        widths.append(min(width, limit))

    header_line = "  ".join(
        yellow(pad(truncate(str(header), limit), widths[index]))
        for index, header in enumerate(headers)
    )
    info(header_line)
    info(dim("  ".join("─" * width for width in widths)))
    for row in cells:
        info("  ".join(pad(row[index], widths[index]) for index in range(len(headers))).rstrip())


# ===== SQLite 访问层 =====
class Store:
    def __init__(self, path: Path) -> None:
        if not path.exists():
            raise ArgError(
                f"数据库不存在：{path}\n"
                "请确认服务端已启动并生成数据库，或用 --db 指定正确路径。"
            )
        self.path = path
        try:
            self.conn = sqlite3.connect(str(path), timeout=5.0)
        except sqlite3.Error as exc:
            raise ArgError(f"打开数据库失败：{exc}") from exc
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'documents'"
        ).fetchone()
        if not row:
            self.conn.close()
            raise ArgError(f"该文件不是汇英斋数据库（缺少 documents 表）：{path}")

    # ---- 基础查询 ----
    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def x(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cursor = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return cursor

    def close(self) -> None:
        self.conn.close()

    # ---- 文档读写（与 server/app/db.py 行为一致）----
    @staticmethod
    def _parse(raw: Any, fallback_id: Optional[str] = None) -> Optional[dict]:
        try:
            doc = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        if fallback_id:
            doc.setdefault("_id", fallback_id)
        return doc

    def collections(self) -> list[tuple[str, int]]:
        rows = self.q(
            "SELECT collection, COUNT(*) AS n FROM documents "
            "GROUP BY collection ORDER BY collection"
        )
        return [(row["collection"], int(row["n"])) for row in rows]

    def collection_exists(self, collection: str) -> bool:
        row = self.q(
            "SELECT 1 FROM documents WHERE collection = ? LIMIT 1", (collection,)
        )
        return bool(row)

    def docs(self, collection: str) -> list[dict]:
        rows = self.q(
            "SELECT id, data FROM documents WHERE collection = ?", (collection,)
        )
        result = []
        for row in rows:
            doc = self._parse(row["data"], row["id"])
            if doc is not None:
                result.append(doc)
        return result

    def get(self, collection: str, doc_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT id, data FROM documents WHERE collection = ? AND id = ?",
            (collection, str(doc_id)),
        ).fetchone()
        return self._parse(row["data"], row["id"]) if row else None

    def id_exists(self, collection: str, doc_id: str) -> bool:
        return bool(
            self.q(
                "SELECT 1 FROM documents WHERE collection = ? AND id = ? LIMIT 1",
                (collection, str(doc_id)),
            )
        )

    def insert(self, collection: str, doc: dict) -> dict:
        payload = dict(doc)
        doc_id = str(payload.get("_id") or uuid.uuid4())
        payload["_id"] = doc_id
        self.x(
            "INSERT INTO documents (collection, id, data, created_at) VALUES (?, ?, ?, ?)",
            (
                collection,
                doc_id,
                json.dumps(payload, ensure_ascii=False),
                now_ms(),
            ),
        )
        return payload

    def save(self, collection: str, doc: dict) -> None:
        doc_id = str(doc["_id"])
        cursor = self.x(
            "UPDATE documents SET data = ? WHERE collection = ? AND id = ?",
            (json.dumps(doc, ensure_ascii=False), collection, doc_id),
        )
        if cursor.rowcount == 0:
            raise ArgError(f"文档不存在或已被删除：{collection}/{doc_id}")

    def delete(self, collection: str, doc_id: str) -> bool:
        cursor = self.x(
            "DELETE FROM documents WHERE collection = ? AND id = ?",
            (collection, str(doc_id)),
        )
        return cursor.rowcount > 0

    def delete_many(self, collection: str, doc_ids: list[str]) -> int:
        if not doc_ids:
            return 0
        with self.conn:
            self.conn.executemany(
                "DELETE FROM documents WHERE collection = ? AND id = ?",
                [(collection, doc_id) for doc_id in doc_ids],
            )
        return len(doc_ids)

    def delete_collection(self, collection: str) -> int:
        cursor = self.x("DELETE FROM documents WHERE collection = ?", (collection,))
        return cursor.rowcount

    def backup_to(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(str(target))
        try:
            self.conn.backup(destination)
        finally:
            destination.close()

    def table_counts(self) -> list[tuple[str, int]]:
        rows = self.q("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        result = []
        for row in rows:
            name = row["name"]
            count = self.q(f'SELECT COUNT(*) AS n FROM "{name}"')[0]["n"]
            result.append((name, int(count)))
        return result


def open_store() -> Store:
    global STORE
    resolve_settings()
    STORE = Store(SETTINGS.db_path)  # type: ignore[arg-type]
    return STORE


def require_store() -> Store:
    if STORE is None:
        return open_store()
    return STORE


def require_write() -> None:
    if SETTINGS.readonly:
        raise ArgError("当前是只读模式（--readonly），不能修改数据库。")


# ===== 参数与值解析 =====
COND_RE = re.compile(r"^([A-Za-z0-9_.\-]+)(~=|!=|>=|<=|=|>|<)(.*)$", re.S)
ASSIGN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)=(.*)$", re.S)
READ_SQL_RE = re.compile(r"^\s*(select|explain|pragma)\b", re.I)


def tokenize(line: str) -> list[str]:
    """交互模式分词：支持单/双引号，反斜杠保持原样（方便 Windows 路径）。"""
    tokens: list[str] = []
    current: list[str] = []
    quote: Optional[str] = None
    started = False
    for char in line:
        if quote:
            if char == quote:
                quote = None
            else:
                current.append(char)
        elif char in "\"'":
            quote = char
            started = True
        elif char.isspace():
            if started:
                tokens.append("".join(current))
                current = []
                started = False
        else:
            current.append(char)
            started = True
    if quote:
        raise ArgError("引号未闭合")
    if started:
        tokens.append("".join(current))
    return tokens


def parse_query_value(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("s:"):
        return text[2:]
    if text == "":
        return ""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return raw


def coerce_value(raw: str, old: Any) -> Any:
    """`set` 时按原字段类型转换，避免学号、纯数字字符串被误改成数字。"""
    if raw.startswith("s:"):
        return raw[2:]
    if isinstance(old, bool):
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "是"):
            return True
        if low in ("false", "0", "no", "否"):
            return False
        raise ArgError("该字段是布尔值，请填 true / false")
    if isinstance(old, int):
        try:
            return int(raw.strip())
        except ValueError as exc:
            raise ArgError(f"该字段是整数，无法解析：{raw}") from exc
    if isinstance(old, float):
        try:
            return float(raw.strip())
        except ValueError as exc:
            raise ArgError(f"该字段是数字，无法解析：{raw}") from exc
    if isinstance(old, (list, dict)):
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ArgError(f"该字段是 JSON 数组/对象，请提供合法 JSON：{raw}") from exc
    if isinstance(old, str):
        return raw
    return parse_query_value(raw)


def get_path(doc: Any, path: str) -> Any:
    current = doc
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return MISSING
            current = current[part]
        elif isinstance(current, list):
            try:
                index = int(part)
            except ValueError:
                return MISSING
            if index < 0 or index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return current


def set_path(doc: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    current: Any = doc
    for index, part in enumerate(parts[:-1]):
        next_part = parts[index + 1]
        if isinstance(current, dict):
            if part not in current:
                current[part] = [] if next_part.isdigit() else {}
            current = current[part]
        elif isinstance(current, list):
            try:
                position = int(part)
            except ValueError as exc:
                raise ArgError(f"无法定位字段：{path}") from exc
            if position < 0 or position >= len(current):
                raise ArgError(f"数组下标越界：{path}")
            current = current[position]
        else:
            raise ArgError(f"无法定位字段：{path}（{part} 的上级不是对象）")

    last = parts[-1]
    if isinstance(current, dict):
        current[last] = value
    elif isinstance(current, list):
        try:
            position = int(last)
        except ValueError as exc:
            raise ArgError(f"无法定位字段：{path}") from exc
        if position < 0 or position >= len(current):
            raise ArgError(f"数组下标越界：{path}")
        current[position] = value
    else:
        raise ArgError(f"无法设置字段：{path}")


def unset_path(doc: Any, path: str) -> bool:
    parts = path.split(".")
    current = doc
    for part in parts[:-1]:
        if isinstance(current, dict):
            if part not in current:
                return False
            current = current[part]
        elif isinstance(current, list):
            try:
                index = int(part)
            except ValueError:
                return False
            if index < 0 or index >= len(current):
                return False
            current = current[index]
        else:
            return False

    last = parts[-1]
    if isinstance(current, dict):
        if last in current:
            del current[last]
            return True
        return False
    if isinstance(current, list):
        try:
            index = int(last)
        except ValueError:
            return False
        if index < 0 or index >= len(current):
            return False
        del current[index]
        return True
    return False


def looks_like_condition(text: str) -> bool:
    return bool(COND_RE.match(text)) or (
        text.startswith("~") and bool(ASSIGN_RE.match(text[1:]))
    )


def parse_condition(text: str) -> tuple[str, str, Any]:
    # 支持两种写法：~content=关键词 或 content~=关键词
    if text.startswith("~"):
        match = ASSIGN_RE.match(text[1:])
        if match:
            return match.group(1), "~=", parse_query_value(match.group(2))
    match = COND_RE.match(text)
    if not match:
        raise ArgError(
            f"条件格式应为 字段=值（支持 ~= != >= <= > <）：{text}\n"
            "例如：find roster position=班长"
        )
    return match.group(1), match.group(2), parse_query_value(match.group(3))


def _comparable(actual: Any, expected: Any) -> tuple[Any, Any]:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return str(actual).lower(), str(expected).lower()
    if isinstance(actual, (int, float)) and not isinstance(actual, bool):
        try:
            return actual, float(expected)
        except (TypeError, ValueError):
            pass
    elif isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return float(actual), expected
        except (TypeError, ValueError):
            pass
    return str(actual), str(expected)


def match_condition(doc: dict, condition: tuple[str, str, Any]) -> bool:
    path, op, expected = condition
    actual = get_path(doc, path)

    if actual is MISSING:
        return op == "!="
    if op == "~=":
        if actual is None:
            return False
        haystack = (
            json.dumps(actual, ensure_ascii=False)
            if isinstance(actual, (list, dict))
            else str(actual)
        )
        return str(expected).lower() in haystack.lower()
    if op == "=":
        if actual is None or expected is None:
            return actual is expected
        left, right = _comparable(actual, expected)
        return left == right
    if op == "!=":
        if actual is None or expected is None:
            return actual is not expected
        left, right = _comparable(actual, expected)
        return left != right

    if actual is None or expected is None:
        return False
    left, right = _comparable(actual, expected)
    try:
        if op == ">":
            return left > right
        if op == "<":
            return left < right
        if op == ">=":
            return left >= right
        if op == "<=":
            return left <= right
    except TypeError:
        left, right = str(left), str(right)
        return {
            ">": left > right,
            "<": left < right,
            ">=": left >= right,
            "<=": left <= right,
        }[op]
    return False


def sort_key(doc: dict, path: str) -> tuple[int, Any]:
    value = get_path(doc, path)
    if value is MISSING or value is None:
        return (2, "")
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (0, value)
    return (1, str(value))


def parse_query_args(
    tokens: list[str], default_limit: int = 20
) -> tuple[list[tuple[str, str, Any]], dict[str, Any]]:
    conditions: list[tuple[str, str, Any]] = []
    options: dict[str, Any] = {
        "limit": default_limit,
        "sort": None,
        "desc": True,
        "json": False,
    }
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("--limit", "-n"):
            index += 1
            if index >= len(tokens):
                raise ArgError("--limit 缺少数值")
            options["limit"] = _to_limit(tokens[index])
        elif token.startswith("--limit="):
            options["limit"] = _to_limit(token.split("=", 1)[1])
        elif token in ("--sort", "-s"):
            index += 1
            if index >= len(tokens):
                raise ArgError("--sort 缺少字段名")
            options["sort"] = tokens[index]
        elif token.startswith("--sort="):
            options["sort"] = token.split("=", 1)[1]
        elif token == "--asc":
            options["desc"] = False
        elif token == "--desc":
            options["desc"] = True
        elif token == "--json":
            options["json"] = True
        elif token == "--all":
            options["limit"] = 0
        elif token.isdigit():
            options["limit"] = int(token)
        elif looks_like_condition(token):
            conditions.append(parse_condition(token))
        else:
            raise ArgError(f"无法识别的参数：{token}（用 help 查看用法）")
        index += 1
    return conditions, options


def _to_limit(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ArgError(f"数量必须是整数：{raw}") from exc
    if value < 0:
        raise ArgError("数量不能为负数")
    return value


def apply_query(
    docs: list[dict], conditions: list[tuple[str, str, Any]], options: dict[str, Any]
) -> tuple[list[dict], int]:
    rows = [doc for doc in docs if all(match_condition(doc, c) for c in conditions)]
    sort_field = options["sort"]
    if sort_field is None and any(
        isinstance(doc.get("createTime"), (int, float)) for doc in rows
    ):
        sort_field = "createTime"
    if sort_field:
        present = []
        missing = []
        for doc in rows:
            value = get_path(doc, sort_field)
            if value is MISSING or value is None:
                missing.append(doc)
            else:
                present.append(doc)
        present.sort(key=lambda doc: sort_key(doc, sort_field), reverse=options["desc"])
        rows = present + missing
    total = len(rows)
    if options["limit"] > 0:
        rows = rows[: options["limit"]]
    return rows, total


# ===== 文档展示 =====
PREFERRED_COLUMNS: dict[str, list[str]] = {
    "users": ["name", "studentId", "className", "role", "position", "registered"],
    "roster": ["name", "studentId", "position", "repSubjects"],
    "subject_settings": ["subject", "teacher", "credit", "schedule", "updaterName"],
    "subject_tasks": ["subject", "title", "deadline", "creatorName", "createTime"],
    "homeworks": ["title", "deadline", "creatorName", "createTime"],
    "notifications": ["type", "title", "deadline", "creatorName", "createTime"],
    "activities": ["title", "place", "activityTime", "creatorName", "createTime"],
    "posts": ["nickname", "content", "replyCount", "createTime"],
    "post_replies": ["nickname", "content", "postId", "createTime"],
    "subscriptions": ["_openid", "remain", "updateTime", "createTime"],
}


def columns_for(collection: str, docs: list[dict]) -> list[str]:
    preferred = PREFERRED_COLUMNS.get(collection)
    if preferred:
        return ["_id"] + [key for key in preferred if key != "_id"]
    keys: list[str] = []
    for doc in docs:
        for key in doc:
            if key not in keys:
                keys.append(key)
    keys = [key for key in keys if key not in ("_openid",) and key != "_id"][:6]
    return ["_id"] + keys


def print_docs(collection: str, docs: list[dict], as_json: bool = False) -> None:
    if as_json or SETTINGS.json_out:
        print(json.dumps(docs, ensure_ascii=False, indent=2))
        return
    if not docs:
        info(dim("（空）"))
        return

    columns = columns_for(collection, docs)
    headers = [("ID" if key == "_id" else key) for key in columns]
    rows = []
    for doc in docs:
        cells = []
        for key in columns:
            if key == "_id":
                value = str(doc.get("_id", ""))
                cells.append(value[:8])
            else:
                cells.append(cell_text(key, get_path(doc, key)))
        rows.append(cells)
    print_table(headers, rows)


def iter_file_ids(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        file_id = value.get("fileID")
        if isinstance(file_id, str) and file_id.startswith("/uploads/"):
            yield file_id
        for child in value.values():
            yield from iter_file_ids(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_file_ids(child)


def delete_attachment_files(doc: dict) -> tuple[int, int]:
    """删除文档引用的物理附件，返回（已删除，失败）。"""
    if SETTINGS.upload_dir is None:
        return (0, 0)
    root = SETTINGS.upload_dir.resolve()
    removed = 0
    failed = 0
    for file_id in set(iter_file_ids(doc)):
        relative = file_id[len("/uploads/") :]
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            warn(f"跳过非法附件路径：{file_id}")
            failed += 1
            continue
        try:
            target.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            warn(f"删除附件失败：{target}（{exc}）")
            failed += 1
    return removed, failed


def confirm(prompt: str, force: bool = False) -> bool:
    if force or SETTINGS.yes:
        return True
    if not sys.stdin.isatty():
        error("这是不可撤销的操作；非交互环境请加 --yes 明确确认。")
        return False
    try:
        answer = input(f"{yellow('?')} {prompt} (y/N) ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in ("y", "yes", "是")


def resolve_id(store: Store, collection: str, token: str) -> str:
    token = str(token)
    if store.id_exists(collection, token):
        return token
    escaped = (
        token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    )
    rows = store.q(
        "SELECT id FROM documents WHERE collection = ? AND id LIKE ? ESCAPE '\\'",
        (collection, escaped),
    )
    ids = [row["id"] for row in rows]
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise ArgError(f"集合 {collection} 中找不到 ID 为 {token} 的文档")
    raise ArgError(
        "ID 前缀不唯一，请多输入几位：\n  " + "\n  ".join(ids[:10])
    )


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


# ===== 命令实现 =====
def cmd_help(tokens: list[str]) -> None:
    text = f"""
{bold('汇英斋数据库终端')} v{VERSION}

{bold('用法')}
  python dbterm.py                      进入交互终端
  python dbterm.py <命令> [参数]        单次执行
  python dbterm.py --db <路径> <命令>   指定数据库

{bold('全局选项（写在命令前）')}
  --db PATH           SQLite 数据库路径，默认 server/data/huiyingzhai.sqlite
  --server-root DIR   服务端目录，用于读取 .env（DATA_DIR / UPLOAD_DIR）
  --upload-dir DIR    指定附件目录（配合 --with-files 使用）
  -y, --yes           跳过删除类操作的确认
  -r, --readonly      只读模式，禁止任何写入
  --json              查询结果以 JSON 输出
  --no-color          关闭颜色

{bold('查询')}
  status                                  数据库、集合、附件目录概览
  collections                             列出所有集合及文档数
  tables                                  列出 SQLite 表及行数
  ls <集合> [数量] [--sort 字段] [--asc]  浏览集合（默认最近 20 条）
  show <集合> <ID>                        查看完整 JSON（ID 可用前缀）
  find <集合> [条件...] [--limit N]       条件查询，例如：
       find roster position=班长
       find users ~className=计算机
       find homeworks deadline>1700000000000
       find posts --json --limit 5

{bold('修改')}
  set <集合> <ID> 字段=值 [字段=值 ...]   修改字段（按原字段类型自动转换）
  unset <集合> <ID> 字段 [字段...]        删除字段
  insert <集合> '<JSON>'                  插入新文档（也可用 字段=值）
  put <集合> <ID> <base64 JSON>           整份保存文档（图形界面使用）
  rm <集合> <ID> [--with-files]           删除文档（--with-files 同时删附件）
  rmwhere <集合> 条件... [--with-files]   按条件批量删除
  sql <SQL>                               执行任意 SQL（写操作需确认）

{bold('备份 / 导入导出')}
  backup [文件]                           备份整个数据库（默认 backups/ 目录）
  export <集合> [文件]                    导出集合为 JSON
  import <集合> <文件> [--replace]        导入 JSON（按 _id 覆盖，--replace 清空后导入）

{bold('其他')}
  help [命令] / ?                         帮助
  exit / quit / q                         退出

{bold('多级字段')}  用点号访问嵌套字段，例如 attachments.0.name、attachments.0.type
{bold('字符串值')}  纯数字字符串可加 s: 前缀，例如 set users <ID> studentId=s:00123
{bold('备份提醒')}  线上修改前先执行 backup；删除/批量修改都会要求确认。
"""
    info(text.strip("\n"))


def cmd_status(tokens: list[str]) -> None:
    store = require_store()
    path = SETTINGS.db_path
    assert path is not None
    size = path.stat().st_size
    journal = store.q("PRAGMA journal_mode")[0][0]
    sqlite_version = store.q("SELECT sqlite_version() AS v")[0]["v"]
    collections = store.collections()

    info(bold("数据库"))
    info(f"  路径      {path}")
    info(f"  大小      {human_size(size)}")
    info(f"  SQLite    {sqlite_version}  (journal={journal})")
    info(f"  服务目录  {SETTINGS.server_root}")
    info(f"  附件目录  {SETTINGS.upload_dir}")
    info("")
    info(bold(f"集合（{len(collections)} 个）"))
    rows = [[name, str(count)] for name, count in collections]
    print_table(["集合", "文档数"], rows)
    info("")
    info(dim("提示：服务端每次请求都会读取数据库，修改后无需重启服务。"))


def cmd_collections(tokens: list[str]) -> None:
    store = require_store()
    collections = store.collections()
    if SETTINGS.json_out:
        print(json.dumps(dict(collections), ensure_ascii=False, indent=2))
        return
    print_table(
        ["集合", "文档数"], [[name, str(count)] for name, count in collections]
    )


def cmd_tables(tokens: list[str]) -> None:
    store = require_store()
    rows = [[name, str(count)] for name, count in store.table_counts()]
    print_table(["表", "行数"], rows)


def cmd_ls(tokens: list[str]) -> None:
    if not tokens:
        raise ArgError("用法：ls <集合> [数量] [--sort 字段] [--asc]")
    collection = tokens[0]
    conditions, options = parse_query_args(tokens[1:], default_limit=20)
    store = require_store()
    docs = store.docs(collection)
    if not docs and not store.collection_exists(collection):
        warn(f"集合 {collection} 不存在或为空")
    rows, total = apply_query(docs, conditions, options)
    if not (options["json"] or SETTINGS.json_out):
        info(
            f"{cyan(collection)}  共 {total} 条"
            + (f"，显示 {len(rows)} 条" if len(rows) < total else "")
        )
    print_docs(collection, rows, options["json"])


def cmd_find(tokens: list[str]) -> None:
    if not tokens:
        raise ArgError("用法：find <集合> [字段=值 ...] [--limit N] [--json]")
    collection = tokens[0]
    conditions, options = parse_query_args(tokens[1:], default_limit=20)
    store = require_store()
    docs = store.docs(collection)
    rows, total = apply_query(docs, conditions, options)
    if not (options["json"] or SETTINGS.json_out):
        info(
            f"{cyan(collection)}  匹配 {total} 条"
            + (f"，显示 {len(rows)} 条" if len(rows) < total else "")
        )
    print_docs(collection, rows, options["json"])


def cmd_show(tokens: list[str]) -> None:
    if len(tokens) < 2:
        raise ArgError("用法：show <集合> <ID>")
    store = require_store()
    collection = tokens[0]
    doc_id = resolve_id(store, collection, tokens[1])
    doc = store.get(collection, doc_id)
    if not doc:
        raise ArgError(f"文档不存在：{collection}/{doc_id}")
    info(dim(f"{collection}/{doc_id}"))
    print(json.dumps(doc, ensure_ascii=False, indent=2))


def cmd_set(tokens: list[str]) -> None:
    if len(tokens) < 3:
        raise ArgError("用法：set <集合> <ID> 字段=值 [字段=值 ...]")
    require_write()
    store = require_store()
    collection = tokens[0]
    doc_id = resolve_id(store, collection, tokens[1])
    doc = store.get(collection, doc_id)
    if not doc:
        raise ArgError(f"文档不存在：{collection}/{doc_id}")

    changes: list[tuple[str, Any, Any]] = []
    for token in tokens[2:]:
        match = ASSIGN_RE.match(token)
        if not match:
            raise ArgError(f"参数格式应为 字段=值：{token}")
        path = match.group(1)
        if path.split(".")[0] == "_id":
            raise ArgError("不能修改 _id；如需更换 ID 请导出后重新插入。")
        old = get_path(doc, path)
        new = coerce_value(match.group(2), old)
        set_path(doc, path, new)
        changes.append((path, old, new))

    store.save(collection, doc)
    ok(f"已更新 {collection}/{doc_id}")
    for path, old, new in changes:
        old_text = "<不存在>" if old is MISSING else json.dumps(old, ensure_ascii=False)
        new_text = json.dumps(new, ensure_ascii=False)
        info(f"  {path}: {dim(old_text)} → {green(new_text)}")


def cmd_unset(tokens: list[str]) -> None:
    if len(tokens) < 3:
        raise ArgError("用法：unset <集合> <ID> 字段 [字段 ...]")
    require_write()
    store = require_store()
    collection = tokens[0]
    doc_id = resolve_id(store, collection, tokens[1])
    doc = store.get(collection, doc_id)
    if not doc:
        raise ArgError(f"文档不存在：{collection}/{doc_id}")

    removed = []
    for path in tokens[2:]:
        if path.split(".")[0] == "_id":
            raise ArgError("不能删除 _id。")
        if unset_path(doc, path):
            removed.append(path)
        else:
            warn(f"字段不存在，已跳过：{path}")

    if not removed:
        return
    store.save(collection, doc)
    ok(f"已更新 {collection}/{doc_id}，删除字段：{', '.join(removed)}")


def cmd_insert(tokens: list[str]) -> None:
    if len(tokens) < 2:
        raise ArgError("用法：insert <集合> '<JSON>' 或 insert <集合> 字段=值 ...")
    require_write()
    store = require_store()
    collection = tokens[0]
    rest = tokens[1:]

    if rest[0].lstrip().startswith("{"):
        raw = " ".join(rest)
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise ArgError(f"JSON 解析失败：{exc}") from exc
        if not isinstance(payload, dict):
            raise ArgError("插入内容必须是 JSON 对象（{...}）")
    else:
        payload = {}
        for token in rest:
            match = ASSIGN_RE.match(token)
            if not match:
                raise ArgError(f"参数格式应为 字段=值：{token}")
            payload[match.group(1)] = parse_query_value(match.group(2))

    doc_id = str(payload.get("_id") or "")
    if doc_id and store.id_exists(collection, doc_id):
        raise ArgError(f"ID 已存在：{doc_id}（可用 set 修改现有文档）")

    inserted = store.insert(collection, payload)
    ok(f"已插入 {collection}/{inserted['_id']}")
    if "createTime" not in inserted:
        info(dim("提示：该文档没有 createTime，网页会按默认顺序展示。"))


def cmd_put(tokens: list[str]) -> None:
    """用 base64 编码的 JSON 整份保存文档（供图形界面调用，避免命令行转义问题）。"""
    if len(tokens) < 3:
        raise ArgError("用法：put <集合> <ID> <base64 JSON>")
    require_write()
    collection, doc_id, encoded = tokens[0], tokens[1], tokens[2]
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ArgError(f"文档内容不是合法的 base64：{exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArgError(f"文档内容不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ArgError("文档内容必须是 JSON 对象（{...}）")

    existing = str(payload.get("_id") or "")
    if existing and existing != doc_id:
        raise ArgError(f"文档 _id（{existing}）与命令 ID（{doc_id}）不一致")
    payload["_id"] = doc_id

    store = require_store()
    inserted = not store.id_exists(collection, doc_id)
    if inserted:
        store.insert(collection, payload)
    else:
        store.save(collection, payload)

    if SETTINGS.json_out:
        print(
            json.dumps(
                {"ok": True, "id": doc_id, "inserted": inserted}, ensure_ascii=False
            )
        )
    else:
        ok(f"已{'插入' if inserted else '保存'} {collection}/{doc_id}")


def _collect_docs_for_delete(
    collection: str, conditions: list[tuple[str, str, Any]]
) -> list[dict]:
    store = require_store()
    docs = store.docs(collection)
    return [doc for doc in docs if all(match_condition(doc, c) for c in conditions)]


def _extract_flags(tokens: list[str], flags: tuple[str, ...]) -> tuple[list[str], set[str]]:
    found = {token for token in tokens if token in flags}
    return [token for token in tokens if token not in flags], found


def cmd_rm(tokens: list[str]) -> None:
    tokens, flags = _extract_flags(tokens, ("--with-files", "--yes", "-y"))
    with_files = "--with-files" in flags
    force = bool(flags & {"--yes", "-y"})
    if len(tokens) < 2:
        raise ArgError("用法：rm <集合> <ID> [--with-files] [--yes]")
    require_write()
    store = require_store()
    collection = tokens[0]
    doc_id = resolve_id(store, collection, tokens[1])
    doc = store.get(collection, doc_id)
    if not doc:
        raise ArgError(f"文档不存在：{collection}/{doc_id}")

    if not force:
        info(dim(f"将删除 {collection}/{doc_id}"))
        print(json.dumps(doc, ensure_ascii=False, indent=2)[:2000])
    if not confirm("确认删除该文档？", force=force):
        info("已取消。")
        return

    if not store.delete(collection, doc_id):
        raise ArgError("删除失败：文档可能已被其他进程删除")
    ok(f"已删除 {collection}/{doc_id}")
    if with_files:
        removed, failed = delete_attachment_files(doc)
        info(f"附件：删除 {removed} 个" + (f"，失败 {failed} 个" if failed else ""))


def cmd_rmwhere(tokens: list[str]) -> None:
    tokens, flags = _extract_flags(tokens, ("--with-files", "--yes", "-y"))
    with_files = "--with-files" in flags
    force = bool(flags & {"--yes", "-y"})
    if not tokens:
        raise ArgError("用法：rmwhere <集合> 字段=值 [字段=值 ...] [--with-files] [--yes]")
    require_write()
    collection = tokens[0]
    conditions, options = parse_query_args(tokens[1:], default_limit=0)
    if options["limit"]:
        warn("rmwhere 会删除全部匹配项，--limit 只影响查询、不限制删除数量。")
    if not conditions:
        raise ArgError("为避免误删整表，rmwhere 至少需要一个条件。")
    docs = _collect_docs_for_delete(collection, conditions)
    if not docs:
        info("没有匹配的文档。")
        return

    info(f"将删除 {cyan(collection)} 中 {bold(str(len(docs)))} 条文档：")
    preview = docs[:10]
    for doc in preview:
        title = (
            doc.get("title")
            or doc.get("name")
            or doc.get("nickname")
            or (str(doc.get("content", ""))[:30])
        )
        info(f"  {str(doc.get('_id', ''))[:8]}  {title}")
    if len(docs) > len(preview):
        info(dim(f"  …… 其余 {len(docs) - len(preview)} 条"))
    if not confirm(f"确认批量删除 {len(docs)} 条文档？", force=force):
        info("已取消。")
        return

    ids = [str(doc["_id"]) for doc in docs]
    store = require_store()
    count = store.delete_many(collection, ids)
    ok(f"已删除 {count} 条文档")
    if with_files:
        removed = failed = 0
        for doc in docs:
            part_removed, part_failed = delete_attachment_files(doc)
            removed += part_removed
            failed += part_failed
        info(f"附件：删除 {removed} 个" + (f"，失败 {failed} 个" if failed else ""))


def cmd_export(tokens: list[str]) -> None:
    if not tokens:
        raise ArgError("用法：export <集合> [文件路径]")
    store = require_store()
    collection = tokens[0]
    if len(tokens) >= 2:
        target = Path(tokens[1]).expanduser()
        if not target.is_absolute():
            target = (Path.cwd() / target).resolve()
    else:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = WORK_DIR / "exports" / f"{collection}-{stamp}.json"

    docs = store.docs(collection)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(docs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    ok(f"已导出 {len(docs)} 条到 {target}")


def _read_import_file(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ArgError(f"读取文件失败：{exc}") from exc
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
        if text.lstrip().startswith(("[", "{")):
            raise ArgError("导入文件不是合法 JSON")
        # JSON Lines
        rows = []
        for number, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError as exc:
                raise ArgError(f"第 {number} 行不是合法 JSON：{exc}") from exc
        payload = rows

    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            payload = payload["data"]
        else:
            payload = [payload]
    if not isinstance(payload, list):
        raise ArgError("导入文件应为 JSON 数组、{data: [...]} 或 JSON Lines")
    rows = []
    for index, item in enumerate(payload, 1):
        if not isinstance(item, dict):
            raise ArgError(f"第 {index} 条不是 JSON 对象")
        rows.append(item)
    return rows


def cmd_import(tokens: list[str]) -> None:
    tokens, flags = _extract_flags(tokens, ("--replace", "--yes", "-y"))
    replace = "--replace" in flags
    force = bool(flags & {"--yes", "-y"})
    if len(tokens) < 2:
        raise ArgError("用法：import <集合> <文件> [--replace] [--yes]")
    require_write()
    store = require_store()
    collection = tokens[0]
    path = Path(tokens[1]).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    rows = _read_import_file(path)

    existing_ids = {str(doc.get("_id")) for doc in store.docs(collection)}
    if replace:
        created, updated = len(rows), 0
    else:
        created = sum(
            1 for row in rows if not row.get("_id") or str(row["_id"]) not in existing_ids
        )
        updated = len(rows) - created
    info(f"导入 {cyan(collection)}：共 {len(rows)} 条（新增 {created}，更新 {updated}）")
    if replace:
        warn(f"--replace 会先清空集合中的全部 {len(existing_ids)} 条现有文档！")
    if not confirm("确认导入？", force=force):
        info("已取消。")
        return

    if replace:
        store.delete_collection(collection)

    with store.conn:
        for row in rows:
            doc = dict(row)
            doc_id = str(doc.get("_id") or "") or str(uuid.uuid4())
            doc["_id"] = doc_id
            store.conn.execute(
                "INSERT INTO documents (collection, id, data, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(collection, id) DO UPDATE SET data = excluded.data",
                (
                    collection,
                    doc_id,
                    json.dumps(doc, ensure_ascii=False),
                    now_ms(),
                ),
            )
    ok(f"导入完成：{collection} 现有 {len(store.docs(collection))} 条")


def cmd_sql(tokens: list[str]) -> None:
    if not tokens:
        raise ArgError('用法：sql "<SQL>" [--json]')
    tokens, flags = _extract_flags(tokens, ("--json", "--yes", "-y"))
    as_json = "--json" in flags
    force = bool(flags & {"--yes", "-y"})
    if not tokens:
        raise ArgError("缺少 SQL 语句")
    sql = " ".join(tokens).strip()
    store = require_store()

    if SETTINGS.readonly and not re.match(r"^\s*(select|explain)\b", sql, re.I):
        raise ArgError("只读模式（--readonly）下只能执行 SELECT / EXPLAIN。")

    is_read = bool(READ_SQL_RE.match(sql))
    if not is_read:
        require_write()
        info(bold("即将执行写操作："))
        info("  " + sql)
        if not confirm("确认执行？", force=force):
            info("已取消。")
            return

    try:
        cursor = store.conn.execute(sql)
        if is_read and cursor.description:
            headers = [description[0] for description in cursor.description]
            rows = cursor.fetchall()
            if as_json or SETTINGS.json_out:
                print(
                    json.dumps(
                        [dict(zip(headers, tuple(row))) for row in rows],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                body = []
                for row in rows[:500]:
                    body.append(
                        [
                            cell_text(
                                headers[index],
                                row[index] if index < len(row) else None,
                            )
                            for index in range(len(headers))
                        ]
                    )
                print_table(headers, body)
                if len(rows) > 500:
                    info(dim(f"（共 {len(rows)} 行，仅显示前 500 行）"))
                else:
                    info(dim(f"（{len(rows)} 行）"))
            return

        store.conn.commit()
        message = f"执行完成，影响 {cursor.rowcount} 行"
        if cursor.lastrowid:
            message += f"，lastrowid={cursor.lastrowid}"
        ok(message)
    except sqlite3.Error as exc:
        store.conn.rollback()
        raise ArgError(f"SQL 执行失败：{exc}") from exc


def cmd_backup(tokens: list[str]) -> None:
    tokens, flags = _extract_flags(tokens, ("--yes", "-y"))
    force = bool(flags & {"--yes", "-y"})
    store = require_store()
    if tokens:
        target = Path(tokens[0]).expanduser()
        if not target.is_absolute():
            target = (Path.cwd() / target).resolve()
    else:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = WORK_DIR / "backups" / f"huiyingzhai-{stamp}.sqlite"

    if target.exists() and not confirm(f"文件已存在，覆盖？{target}", force=force):
        info("已取消。")
        return
    store.backup_to(target)
    ok(f"已备份到 {target}（{human_size(target.stat().st_size)}）")


def cmd_exit(tokens: list[str]) -> None:
    raise SystemExit(0)


COMMANDS = {
    "help": cmd_help,
    "?": cmd_help,
    "status": cmd_status,
    "info": cmd_status,
    "collections": cmd_collections,
    "colls": cmd_collections,
    "tables": cmd_tables,
    "ls": cmd_ls,
    "list": cmd_ls,
    "find": cmd_find,
    "show": cmd_show,
    "get": cmd_show,
    "set": cmd_set,
    "unset": cmd_unset,
    "insert": cmd_insert,
    "put": cmd_put,
    "rm": cmd_rm,
    "remove": cmd_rm,
    "delete": cmd_rm,
    "rmwhere": cmd_rmwhere,
    "export": cmd_export,
    "import": cmd_import,
    "sql": cmd_sql,
    "backup": cmd_backup,
    "exit": cmd_exit,
    "quit": cmd_exit,
    "q": cmd_exit,
}


def dispatch(tokens: list[str]) -> None:
    command = tokens[0].lower()
    handler = COMMANDS.get(command)
    if not handler:
        raise ArgError(f"未知命令：{tokens[0]}。输入 help 查看全部命令。")
    handler(tokens[1:])


# ===== 入口 =====
def print_banner() -> None:
    path = SETTINGS.db_path
    info("")
    info(bold(f"  汇英斋数据库终端 v{VERSION}"))
    info(f"  数据库  {path}")
    info(f"  服务目录  {SETTINGS.server_root}")
    if SETTINGS.readonly:
        info(yellow("  只读模式：所有写入命令已禁用"))
    info("")
    info(dim("  输入 help 查看命令，collections 查看集合，exit 退出"))
    info("")


def run_interactive() -> int:
    try:
        import readline  # noqa: F401  Unix 下提供历史记录
    except Exception:
        pass
    print_banner()
    while True:
        try:
            line = input(cyan("dbterm> "))
        except (EOFError, KeyboardInterrupt):
            print()
            break
        try:
            tokens = tokenize(line)
        except ArgError as exc:
            error(str(exc))
            continue
        if not tokens:
            continue
        try:
            dispatch(tokens)
        except SystemExit:
            break
        except ArgError as exc:
            error(str(exc))
        except sqlite3.Error as exc:
            error(f"数据库错误：{exc}")
        except KeyboardInterrupt:
            print()
        except Exception as exc:  # 交互模式不因单条命令崩溃
            error(f"执行失败：{exc}")
    info("再见。")
    return 0


def parse_global_args(argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    options: dict[str, Any] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ("--db", "--server-root", "--upload-dir"):
            if index + 1 >= len(argv):
                raise ArgError(f"{token} 缺少路径参数")
            options[token] = argv[index + 1]
            index += 2
        elif token.startswith("--db="):
            options["--db"] = token.split("=", 1)[1]
            index += 1
        elif token.startswith("--server-root="):
            options["--server-root"] = token.split("=", 1)[1]
            index += 1
        elif token.startswith("--upload-dir="):
            options["--upload-dir"] = token.split("=", 1)[1]
            index += 1
        elif token in ("-y", "--yes"):
            options["--yes"] = True
            index += 1
        elif token in ("-r", "--readonly"):
            options["--readonly"] = True
            index += 1
        elif token == "--json":
            options["--json"] = True
            index += 1
        elif token == "--no-color":
            options["--no-color"] = True
            index += 1
        elif token in ("-h", "--help"):
            options["--help"] = True
            index += 1
        elif token in ("-v", "--version"):
            options["--version"] = True
            index += 1
        else:
            break
    return options, argv[index:]


def main(argv: list[str]) -> int:
    global COLOR

    try:
        options, rest = parse_global_args(argv)
    except ArgError as exc:
        error(str(exc))
        return 2

    if options.get("--version"):
        print(f"dbterm {VERSION}")
        return 0
    if options.get("--help"):
        cmd_help([])
        return 0

    if options.get("--server-root"):
        SETTINGS.server_root = Path(options["--server-root"]).expanduser().resolve()
    if options.get("--db"):
        SETTINGS.db_path = Path(options["--db"]).expanduser().resolve()
    if options.get("--upload-dir"):
        SETTINGS.upload_dir = Path(options["--upload-dir"]).expanduser().resolve()
    SETTINGS.yes = bool(options.get("--yes"))
    SETTINGS.readonly = bool(options.get("--readonly"))
    SETTINGS.json_out = bool(options.get("--json"))

    color_allowed = (
        "NO_COLOR" not in os.environ
        and not options.get("--no-color")
        and sys.stdout.isatty()
    )
    COLOR = color_allowed and (
        _enable_windows_ansi() or os.environ.get("TERM") not in (None, "", "dumb")
    )

    try:
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    if not rest:
        try:
            open_store()
        except ArgError as exc:
            error(str(exc))
            return 1
        return run_interactive()

    try:
        dispatch(rest)
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except ArgError as exc:
        error(str(exc))
        return 1
    except sqlite3.Error as exc:
        error(f"数据库错误：{exc}")
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        print()
        return 130
    except Exception as exc:
        error(f"执行失败：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
