#!/usr/bin/env python3
# 导入学号名单到 SQLite（支持 JSON 数组或 JSON Lines）
# 用法：python scripts/import_roster.py [文件路径]
# 默认读取仓库根目录的 roster_import.json
from __future__ import annotations

import json
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_ROOT))

from app import db as store  # noqa: E402


def parse_rows(text: str) -> list:
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            if isinstance(parsed.get("data"), list):
                return parsed["data"]
            if parsed.get("studentId"):
                return [parsed]
    except ValueError:
        # 不是合法 JSON，按 JSON Lines 处理
        pass

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    rows = []
    for index, line in enumerate(lines):
        try:
            rows.append(json.loads(line))
        except ValueError:
            raise SystemExit(f"第 {index + 1} 行不是合法 JSON：{line[:60]}")
    return rows


def main() -> None:
    file = Path(sys.argv[1]) if len(sys.argv) > 1 else SERVER_ROOT.parent / "roster_import.json"
    file = file.resolve()
    if not file.exists():
        raise SystemExit(f"名单文件不存在：{file}")

    rows = parse_rows(file.read_text(encoding="utf-8-sig"))
    created = 0
    updated = 0
    skipped = 0

    for row in rows:
        row = row if isinstance(row, dict) else {}
        student_id = str(row.get("studentId") or "").strip()
        name = str(row.get("name") or "").strip()
        if not student_id or not name:
            skipped += 1
            continue

        position = str(row.get("position") or "普通同学").strip() or "普通同学"
        rep_subjects = row.get("repSubjects")
        rep_subjects = (
            [str(item).strip() for item in rep_subjects if str(item).strip()]
            if isinstance(rep_subjects, list)
            else []
        )

        existing = store.find_one("roster", lambda r: r.get("studentId") == student_id)
        if existing:
            store.update(
                "roster",
                existing.get("_id"),
                {
                    "name": name,
                    "position": position,
                    "repSubjects": rep_subjects,
                    "updateTime": store.now_ms(),
                },
            )
            updated += 1
        else:
            store.insert(
                "roster",
                {
                    "studentId": student_id,
                    "name": name,
                    "position": position,
                    "repSubjects": rep_subjects,
                    "createTime": store.now_ms(),
                },
            )
            created += 1

    print(f"名单导入完成：新增 {created}，更新 {updated}，跳过 {skipped}")
    print(f"数据库：{store.db_file}")


if __name__ == "__main__":
    main()
