#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汇英斋数据库图形界面（Windows 远程版）。

复用 dbterm_remote.py 的 SSH 配置与连接逻辑，通过远程执行 dbterm.py
完成查询、编辑、删除、SQL、备份等操作。所有界面代码使用标准库 tkinter，
不依赖第三方包。

启动：双击 dbterm_gui.bat，或执行 python dbterm_gui.py
"""

from __future__ import annotations

import base64
import datetime
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Callable, Optional

# pythonw 启动时 stdout/stderr 可能为 None，先兜底再导入其它模块
TOOL_DIR = Path(__file__).resolve().parent
LOG_FILE = TOOL_DIR / "dbterm_gui.log"
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # type: ignore[assignment]
if sys.stderr is None:
    sys.stderr = open(LOG_FILE, "a", encoding="utf-8")  # type: ignore[assignment]

sys.path.insert(0, str(TOOL_DIR))
import dbterm_remote as remote  # noqa: E402

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, simpledialog, ttk  # noqa: E402

APP_TITLE = "汇英斋数据库管理"
VERSION = "1.0.0"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
READ_SQL_RE = re.compile(r"^\s*(select|explain|pragma)\b", re.I)


# ===== 工具函数 =====
def now_ms() -> int:
    return int(time.time() * 1000)


def is_time_key(key: str) -> bool:
    return key.endswith("Time") or key in ("deadline", "expire_at")


def format_cell(key: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)) and is_time_key(key) and value > 10**11:
        try:
            return datetime.datetime.fromtimestamp(value / 1000).strftime(
                "%Y-%m-%d %H:%M"
            )
        except (OverflowError, OSError, ValueError):
            return str(value)
    if isinstance(value, (list, dict)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    return text.replace("\r", " ").replace("\n", " ").strip()


def clear_tree(tree: ttk.Treeview) -> None:
    items = tree.get_children()
    if items:
        tree.delete(*items)


def doc_summary(doc: dict) -> str:
    for key in ("title", "name", "nickname"):
        value = doc.get(key)
        if value:
            return str(value)
    content = str(doc.get("content") or "").strip()
    return content[:40] or str(doc.get("_id", ""))[:8]


def new_doc_template(collection: str, doc_id: str) -> dict:
    now = now_ms()
    base: dict[str, Any] = {"_id": doc_id, "createTime": now}
    if collection == "users":
        base.update(
            {
                "_openid": "",
                "name": "",
                "studentId": "",
                "className": "",
                "position": "普通同学",
                "role": "student",
                "nickname": "",
                "registered": False,
                "updateTime": now,
            }
        )
    elif collection == "roster":
        base.update(
            {
                "studentId": "",
                "name": "",
                "position": "普通同学",
                "repSubjects": [],
            }
        )
    elif collection == "subject_settings":
        base.update(
            {
                "subject": "",
                "teacher": "",
                "credit": "",
                "schedule": "",
                "updaterName": "",
                "updateTime": now,
            }
        )
    elif collection == "subject_tasks":
        base.update(
            {
                "subject": "",
                "title": "",
                "content": "",
                "deadline": now + 7 * 24 * 3600 * 1000,
                "attachments": [],
                "creatorOpenid": "",
                "creatorName": "",
            }
        )
    elif collection == "homeworks":
        base.update(
            {
                "title": "",
                "desc": "",
                "deadline": now + 7 * 24 * 3600 * 1000,
                "attachments": [],
                "creatorOpenid": "",
                "creatorName": "",
            }
        )
    elif collection == "activities":
        base.update(
            {
                "title": "",
                "place": "待定",
                "content": "",
                "activityTime": now + 7 * 24 * 3600 * 1000,
                "creatorOpenid": "",
                "creatorName": "",
            }
        )
    elif collection == "posts":
        base.update(
            {
                "content": "",
                "attachments": [],
                "_openid": "",
                "nickname": "",
                "role": "student",
                "replyCount": 0,
            }
        )
    elif collection == "post_replies":
        base.update(
            {
                "postId": "",
                "content": "",
                "attachments": [],
                "_openid": "",
                "nickname": "",
                "role": "student",
            }
        )
    elif collection == "notifications":
        base.update(
            {
                "type": "",
                "title": "",
                "content": "",
                "deadline": 0,
                "creatorName": "",
                "publisherOpenid": "",
            }
        )
    elif collection == "subscriptions":
        base.update({"_openid": "", "remain": 0, "updateTime": now})
    return base


# ===== 远程后端 =====
class BackendError(Exception):
    pass


class RemoteSession:
    """把一次 GUI 操作转换成一次远程 dbterm.py 调用。"""

    def __init__(self, config: dict[str, str]) -> None:
        self.config = config
        self._lock = threading.Lock()

    def _connection_hint(self, message: str) -> str:
        if "Host key verification failed" in message:
            message += (
                "\n\n首次连接请先在命令行执行一次 ssh "
                f"{self.config['USER']}@{self.config['HOST']}，"
                "或在 remote.env 中设置 "
                "SSH_OPTIONS=-o StrictHostKeyChecking=accept-new。"
            )
        if "Permission denied" in message or "publickey" in message:
            message += (
                "\n\n图形界面无法输入 SSH 密码，请配置 SSH 密钥登录"
                "（或启动 ssh-agent 后执行 ssh-add）。"
            )
        return message

    def ensure_ready(self) -> None:
        # 先做一次不依赖 scp 的连接探测，给出更明确的排错信息
        probe = remote.ssh_base(self.config, batch=True) + [
            remote.target(self.config),
            "true",
        ]
        try:
            result = subprocess.run(
                probe,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError(f"SSH 连接失败：{exc}") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            raise BackendError(
                self._connection_hint(message or "SSH 连接失败，请检查 remote.env。")
            )
        with redirect_stdout(io.StringIO()):
            remote.upload_script(self.config, quiet=True, batch=True)

    def _execute(self, args: list[str], timeout: int) -> subprocess.CompletedProcess:
        command = remote.wrap_precheck(
            self.config, remote.remote_shell_command(self.config, args)
        )
        ssh_command = remote.ssh_base(self.config, batch=True)
        ssh_command += [remote.target(self.config), command]
        try:
            return subprocess.run(
                ssh_command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError("远程命令超时，请检查网络和服务器状态。") from exc
        except OSError as exc:
            raise BackendError(f"无法启动 ssh/scp：{exc}") from exc

    def _invoke(self, args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
        with self._lock:
            result = self._execute(args, timeout)
            if result.returncode == remote.MISSING_EXIT_CODE:
                with redirect_stdout(io.StringIO()):
                    remote.upload_script(self.config, force=True, quiet=True, batch=True)
                result = self._execute(args, timeout)
            return result

    def run(self, args: list[str], timeout: int = 180) -> str:
        result = self._invoke(args, timeout)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            if not message:
                message = f"远程命令失败（退出码 {result.returncode}）"
            if result.returncode == 255:
                message += (
                    "\n\n提示：图形界面无法输入 SSH 密码，请配置 SSH 密钥登录，"
                    "并确认 remote.env 中的 HOST / USER / SSH_KEY 正确。"
                )
            raise BackendError(self._connection_hint(message))
        return result.stdout

    def _json(self, args: list[str]) -> Any:
        output = self.run(args)
        try:
            return json.loads(output)
        except ValueError as exc:
            snippet = output.strip()[:300] or "（服务器没有输出）"
            raise BackendError(f"服务器返回的内容不是 JSON：\n{snippet}") from exc

    # ---- 业务操作 ----
    def collections(self) -> dict[str, int]:
        data = self._json(["--json", "collections"])
        if not isinstance(data, dict):
            raise BackendError("集合列表格式异常")
        result = {}
        for key, value in data.items():
            try:
                result[str(key)] = int(value)
            except (TypeError, ValueError):
                result[str(key)] = 0
        return result

    def fetch_docs(self, collection: str) -> list[dict]:
        data = self._json(["--json", "find", collection, "--all"])
        if not isinstance(data, list):
            raise BackendError("文档列表格式异常")
        return [item for item in data if isinstance(item, dict)]

    def save_doc(self, collection: str, doc: dict) -> dict:
        doc_id = str(doc.get("_id") or uuid.uuid4())
        doc["_id"] = doc_id
        encoded = base64.b64encode(
            json.dumps(doc, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        output = self.run(["--json", "put", collection, doc_id, encoded])
        try:
            return json.loads(output)
        except ValueError:
            return {"ok": True, "id": doc_id}

    def delete_doc(self, collection: str, doc_id: str) -> str:
        return self.run(["rm", collection, doc_id, "--yes"])

    def sql_read(self, sql: str) -> list[dict]:
        data = self._json(["--json", "sql", sql])
        if not isinstance(data, list):
            raise BackendError("SQL 查询结果格式异常")
        return [item for item in data if isinstance(item, dict)]

    def sql_write(self, sql: str) -> str:
        return self.run(["sql", sql, "--yes"])

    def backup(self, path: str = "") -> str:
        args = ["backup"]
        if path:
            args.append(path)
        args.append("--yes")
        return self.run(args, timeout=300)

    def status(self) -> str:
        return self.run(["status"], timeout=60)

    def download(self, remote_path: str, local_path: str) -> None:
        scp = remote.scp_base(self.config, batch=True)
        if not scp:
            raise BackendError("未找到 scp，无法下载备份。")
        command = scp + [
            f"{remote.target(self.config)}:{remote_path}",
            str(local_path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError(f"下载失败：{exc}") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            raise BackendError(message or f"下载失败（退出码 {result.returncode}）")


# ===== 主窗口 =====
COLUMN_PLANS: dict[str, list[tuple[str, str, int]]] = {
    "users": [
        ("_id", "ID", 100),
        ("name", "姓名", 90),
        ("studentId", "学号", 110),
        ("className", "班级", 130),
        ("role", "角色", 85),
        ("position", "职务", 100),
        ("registered", "已登记", 70),
    ],
    "roster": [
        ("_id", "ID", 100),
        ("name", "姓名", 90),
        ("studentId", "学号", 110),
        ("position", "职务", 100),
        ("repSubjects", "课代表科目", 150),
    ],
    "subject_settings": [
        ("_id", "ID", 100),
        ("subject", "科目", 100),
        ("teacher", "教师", 110),
        ("credit", "学分", 80),
        ("schedule", "上课时间", 170),
        ("updaterName", "更新人", 100),
    ],
    "subject_tasks": [
        ("_id", "ID", 100),
        ("subject", "科目", 90),
        ("title", "标题", 220),
        ("deadline", "截止时间", 140),
        ("creatorName", "发布人", 100),
    ],
    "homeworks": [
        ("_id", "ID", 100),
        ("title", "标题", 240),
        ("deadline", "截止时间", 140),
        ("creatorName", "发布人", 100),
        ("createTime", "创建时间", 140),
    ],
    "notifications": [
        ("_id", "ID", 100),
        ("type", "类型", 110),
        ("title", "标题", 230),
        ("deadline", "截止时间", 140),
        ("creatorName", "发布人", 100),
    ],
    "activities": [
        ("_id", "ID", 100),
        ("title", "标题", 220),
        ("place", "地点", 120),
        ("activityTime", "活动时间", 140),
        ("creatorName", "发布人", 100),
    ],
    "posts": [
        ("_id", "ID", 100),
        ("nickname", "昵称", 100),
        ("content", "内容", 320),
        ("replyCount", "回复", 60),
        ("createTime", "时间", 140),
    ],
    "post_replies": [
        ("_id", "ID", 100),
        ("nickname", "昵称", 100),
        ("content", "内容", 320),
        ("createTime", "时间", 140),
    ],
    "subscriptions": [
        ("_id", "ID", 100),
        ("_openid", "openid", 220),
        ("remain", "剩余次数", 90),
        ("updateTime", "更新时间", 140),
    ],
}


class App(tk.Tk):
    def __init__(
        self,
        config: dict[str, str],
        *,
        auto_connect: bool = True,
        config_path: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self.config_data = config
        self.config_path = Path(config_path) if config_path else remote.CONFIG_FILE
        self.session = RemoteSession(config)

        self.collections: dict[str, int] = {}
        self.docs: list[dict] = []
        self.docs_by_id: dict[str, dict] = {}
        self.current_collection: Optional[str] = None
        self.current_doc_id: Optional[str] = None
        self.editing_id: Optional[str] = None
        self.is_new_doc = False
        self.editor_dirty = False
        self._loading_editor = False
        self._suppress_tree = False
        self.busy = False
        self.action_buttons: list[ttk.Button] = []
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()

        self.title(f"{APP_TITLE} · {config['USER']}@{config['HOST']}")
        self.geometry("1320x820")
        self.minsize(1000, 640)
        self._init_style()
        self._build_menu()
        self._build_ui()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.connection_var.set(f"远程 {config['USER']}@{config['HOST']}")
        self.connection_badge.configure(text=f"SSH · {config['HOST']}")
        self.set_status("准备就绪")
        self.after(50, self._poll_ui_queue)
        if auto_connect:
            self.after(120, self.initial_connect)

    # ---- 外观 ----
    def _init_style(self) -> None:
        style = ttk.Style(self)
        for theme in ("vista", "clam"):
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue
        try:
            import tkinter.font as tkfont

            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
                font = tkfont.nametofont(name)
                if sys.platform == "win32":
                    font.configure(family="Microsoft YaHei UI", size=10)
        except Exception:
            pass
        style.configure("Treeview", rowheight=25)
        style.configure("Status.TLabel", padding=(8, 4))
        style.configure("Error.TLabel", foreground="#b00020", padding=(8, 4))
        style.configure("Success.TLabel", foreground="#146c2e", padding=(8, 4))
        family = "Microsoft YaHei UI" if sys.platform == "win32" else "TkDefaultFont"
        style.configure("Title.TLabel", font=(family, 11, "bold"))
        style.configure("Toolbar.TButton", padding=(10, 4))

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="备份数据库…", command=self.backup_database)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.on_close)
        menubar.add_cascade(label="文件", menu=file_menu)

        conn_menu = tk.Menu(menubar, tearoff=0)
        conn_menu.add_command(label="重新连接", command=self.reconnect)
        conn_menu.add_command(label="数据库状态", command=self.show_status)
        conn_menu.add_separator()
        conn_menu.add_command(label="打开 remote.env", command=self.open_remote_config)
        menubar.add_cascade(label="连接", menu=conn_menu)

        tool_menu = tk.Menu(menubar, tearoff=0)
        tool_menu.add_command(label="新增文档", command=self.new_doc)
        tool_menu.add_command(label="保存文档", command=self.save_doc)
        tool_menu.add_command(label="删除文档", command=self.delete_doc)
        tool_menu.add_separator()
        tool_menu.add_command(label="执行 SQL…", command=self.open_sql_window)
        tool_menu.add_command(label="刷新", command=self.refresh_all)
        menubar.add_cascade(label="工具", menu=tool_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="关于", command=self.show_about)
        menubar.add_cascade(label="帮助", menu=help_menu)

        self.configure(menu=menubar)

    def _build_ui(self) -> None:
        # 工具栏
        toolbar = ttk.Frame(self, padding=(8, 6))
        toolbar.pack(side=tk.TOP, fill=tk.X)
        buttons = [
            ("刷新", self.refresh_all),
            ("新增", self.new_doc),
            ("保存", self.save_doc),
            ("删除", self.delete_doc),
            ("执行 SQL", self.open_sql_window),
            ("备份", self.backup_database),
            ("状态", self.show_status),
        ]
        for index, (label, command) in enumerate(buttons):
            button = ttk.Button(
                toolbar, text=label, command=command, style="Toolbar.TButton"
            )
            button.pack(side=tk.LEFT, padx=(0, 6))
            self.action_buttons.append(button)
            if index in (3, 5):
                ttk.Separator(toolbar, orient=tk.VERTICAL).pack(
                    side=tk.LEFT, fill=tk.Y, padx=(0, 6), pady=2
                )
        self.connection_badge = ttk.Label(toolbar, text="", style="Status.TLabel")
        self.connection_badge.pack(side=tk.RIGHT)

        # 状态栏先建，底部固定
        status = ttk.Frame(self)
        status.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Separator(status, orient=tk.HORIZONTAL).pack(side=tk.TOP, fill=tk.X)
        self.status_label = ttk.Label(status, text="", style="Status.TLabel")
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=140)
        self.connection_var = tk.StringVar(value="")
        self.connection_label = ttk.Label(
            status, textvariable=self.connection_var, style="Status.TLabel"
        )
        self.connection_label.pack(side=tk.RIGHT, padx=(0, 4))

        # 三栏
        paned = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        paned.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))

        # 左：集合
        left = ttk.Frame(paned, padding=(0, 0, 6, 0))
        ttk.Label(left, text="集合", style="Title.TLabel").pack(anchor="w", pady=(4, 4))
        collections_box = ttk.Frame(left)
        collections_box.pack(fill=tk.BOTH, expand=True)
        self.collection_tree = ttk.Treeview(
            collections_box,
            columns=("count",),
            show="tree headings",
            selectmode="browse",
        )
        self.collection_tree.heading("#0", text="名称")
        self.collection_tree.heading("count", text="数量")
        self.collection_tree.column("#0", width=150, minwidth=100, stretch=True)
        self.collection_tree.column("count", width=55, minwidth=45, anchor="e", stretch=False)
        collection_scroll = ttk.Scrollbar(
            collections_box, orient=tk.VERTICAL, command=self.collection_tree.yview
        )
        self.collection_tree.configure(yscrollcommand=collection_scroll.set)
        self.collection_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        collection_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.collection_tree.bind("<<TreeviewSelect>>", self.on_collection_select)
        paned.add(left, weight=1)

        # 中：文档列表
        middle = ttk.Frame(paned)
        search_row = ttk.Frame(middle)
        search_row.pack(fill=tk.X, pady=(4, 6))
        ttk.Label(search_row, text="搜索").pack(side=tk.LEFT, padx=(0, 6))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_row, textvariable=self.search_var)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(search_row, text="清空", command=lambda: self.search_var.set("")).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        self.search_var.trace_add("write", lambda *_: self.render_docs())

        self.docs_info_label = ttk.Label(middle, text="请选择左侧集合")
        self.docs_info_label.pack(anchor="w", pady=(0, 4))

        docs_box = ttk.Frame(middle)
        docs_box.pack(fill=tk.BOTH, expand=True)
        self.doc_tree = ttk.Treeview(docs_box, show="headings", selectmode="browse")
        docs_y = ttk.Scrollbar(docs_box, orient=tk.VERTICAL, command=self.doc_tree.yview)
        docs_x = ttk.Scrollbar(docs_box, orient=tk.HORIZONTAL, command=self.doc_tree.xview)
        self.doc_tree.configure(yscrollcommand=docs_y.set, xscrollcommand=docs_x.set)
        self.doc_tree.grid(row=0, column=0, sticky="nsew")
        docs_y.grid(row=0, column=1, sticky="ns")
        docs_x.grid(row=1, column=0, sticky="ew")
        docs_box.rowconfigure(0, weight=1)
        docs_box.columnconfigure(0, weight=1)
        self.doc_tree.bind("<<TreeviewSelect>>", self.on_doc_select)
        self.doc_tree.bind("<Button-3>", self.show_doc_menu)
        self.doc_tree.bind("<Delete>", lambda _e: self.delete_doc())
        self.doc_menu = tk.Menu(self, tearoff=0)
        self.doc_menu.add_command(label="复制 ID", command=self.copy_selected_id)
        self.doc_menu.add_command(label="删除文档", command=self.delete_doc)
        paned.add(middle, weight=3)

        # 右：JSON 编辑区
        right = ttk.Frame(paned, padding=(6, 0, 0, 0))
        editor_header = ttk.Frame(right)
        editor_header.pack(fill=tk.X, pady=(4, 4))
        self.editor_title = ttk.Label(
            editor_header, text="文档 JSON", style="Title.TLabel"
        )
        self.editor_title.pack(side=tk.LEFT)
        editor_buttons = ttk.Frame(right)
        editor_buttons.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(editor_buttons, text="保存 (Ctrl+S)", command=self.save_doc).pack(
            side=tk.LEFT
        )
        ttk.Button(editor_buttons, text="格式化", command=self.format_editor).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(editor_buttons, text="复制 ID", command=self.copy_editing_id).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        text_box = ttk.Frame(right)
        text_box.pack(fill=tk.BOTH, expand=True)
        self.editor = tk.Text(
            text_box,
            wrap="none",
            undo=True,
            font=("Consolas", 10),
            padx=8,
            pady=8,
            relief="solid",
            borderwidth=1,
        )
        editor_y = ttk.Scrollbar(text_box, orient=tk.VERTICAL, command=self.editor.yview)
        editor_x = ttk.Scrollbar(text_box, orient=tk.HORIZONTAL, command=self.editor.xview)
        self.editor.configure(yscrollcommand=editor_y.set, xscrollcommand=editor_x.set)
        self.editor.grid(row=0, column=0, sticky="nsew")
        editor_y.grid(row=0, column=1, sticky="ns")
        editor_x.grid(row=1, column=0, sticky="ew")
        text_box.rowconfigure(0, weight=1)
        text_box.columnconfigure(0, weight=1)
        self.editor.bind("<<Modified>>", self.on_editor_modified)
        self.editor.insert("1.0", "")
        self.editor.edit_modified(False)
        paned.add(right, weight=2)

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-r>", lambda _e: self.refresh_all())
        self.bind("<F5>", lambda _e: self.refresh_all())
        self.bind("<Control-s>", lambda _e: self.save_doc())
        self.bind("<Control-n>", lambda _e: self.new_doc())
        self.bind("<Control-e>", lambda _e: self.open_sql_window())
        self.bind("<Control-f>", lambda _e: self.focus_search())

    # ---- 状态/忙碌 ----
    def set_status(self, text: str, kind: str = "normal") -> None:
        style = {
            "error": "Error.TLabel",
            "success": "Success.TLabel",
        }.get(kind, "Status.TLabel")
        self.status_label.configure(text=text, style=style)

    def set_busy(self, busy: bool, text: str = "") -> None:
        self.busy = busy
        for button in self.action_buttons:
            try:
                button.state(["disabled"] if busy else ["!disabled"])
            except tk.TclError:
                pass
        if busy:
            self.progress.pack(side=tk.RIGHT, padx=(0, 8), before=self.connection_label)
            self.progress.start(12)
            if text:
                self.set_status(text)
        else:
            self.progress.stop()
            self.progress.pack_forget()

    def run_async(
        self,
        work: Callable[[], Any],
        on_success: Callable[[Any], None],
        busy_text: str = "正在处理…",
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        if self.busy:
            return
        self.set_busy(True, busy_text)

        def worker() -> None:
            try:
                result = work()
            except Exception as exc:  # 后台线程通过队列交回主线程
                self._ui_queue.put(
                    lambda exc=exc: self.handle_async_error(exc, on_error)
                )
            else:
                self._ui_queue.put(
                    lambda result=result: self.handle_async_done(result, on_success)
                )

        threading.Thread(target=worker, daemon=True).start()

    def _poll_ui_queue(self) -> None:
        """主线程定时取出后台结果，Tkinter 控件只能在主线程操作。"""
        while True:
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception as exc:
                self.show_error(exc)
        try:
            if self.winfo_exists():
                self.after(50, self._poll_ui_queue)
        except tk.TclError:
            pass

    def handle_async_done(self, result: Any, callback: Callable[[Any], None]) -> None:
        self.set_busy(False)
        try:
            callback(result)
        except Exception as exc:
            self.show_error(exc)

    def handle_async_error(
        self, exc: Exception, on_error: Optional[Callable[[Exception], None]] = None
    ) -> None:
        self.set_busy(False)
        self.set_status(f"失败：{exc}", "error")
        if on_error is not None:
            try:
                on_error(exc)
                return
            except Exception:
                pass
        self.show_error(exc)

    def show_error(self, exc: Exception) -> None:
        messagebox.showerror(APP_TITLE, str(exc), parent=self)

    # ---- 连接 ----
    def initial_connect(self) -> None:
        if self.busy:
            return

        def work() -> dict[str, int]:
            self.session.ensure_ready()
            return self.session.collections()

        self.run_async(work, self.on_connected, "正在连接服务器…")

    def on_connected(self, collections: dict[str, int]) -> None:
        self.connection_var.set(
            f"远程 {self.config_data['USER']}@{self.config_data['HOST']}"
        )
        self.apply_collections(collections)
        self.set_status("连接成功", "success")
        if self.collections:
            target = (
                self.current_collection
                if self.current_collection in self.collections
                else ("users" if "users" in self.collections else next(iter(self.collections)))
            )
            self.select_collection(target, load=True)

    def reconnect(self) -> None:
        if self.busy:
            return
        if not self.confirm_discard():
            return
        try:
            config = remote.load_config(self.config_path)
        except remote.ConfigError as exc:
            self.show_error(exc)
            return
        self.config_data = config
        self.session = RemoteSession(config)
        self.collections = {}
        self.docs = []
        self.docs_by_id = {}
        self.current_collection = None
        self.current_doc_id = None
        self.editing_id = None
        self.is_new_doc = False
        clear_tree(self.collection_tree)
        clear_tree(self.doc_tree)
        self.clear_editor()
        self.title(f"{APP_TITLE} · {config['USER']}@{config['HOST']}")
        self.connection_var.set(f"远程 {config['USER']}@{config['HOST']}")
        self.connection_badge.configure(text=f"SSH · {config['HOST']}")
        self.initial_connect()

    def open_remote_config(self) -> None:
        path = self.config_path
        try:
            if not path.exists():
                remote.prepare_config_file(path)
                messagebox.showinfo(
                    APP_TITLE,
                    "已生成 remote.env，请填写后点击「连接 → 重新连接」。",
                    parent=self,
                )
                return
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                remote.open_editor(path)
            self.set_status(f"已打开配置：{path}")
        except Exception as exc:
            self.show_error(exc)

    # ---- 集合/文档 ----
    def apply_collections(self, collections: dict[str, int]) -> None:
        self.collections = collections
        self._suppress_tree = True
        clear_tree(self.collection_tree)
        for name in sorted(collections):
            self.collection_tree.insert(
                "", "end", iid=name, text=name, values=(collections[name],)
            )
        self._suppress_tree = False

    def select_collection(self, name: str, *, load: bool) -> None:
        if name not in self.collections:
            return
        self._suppress_tree = True
        self.collection_tree.selection_set(name)
        self.collection_tree.focus(name)
        self.collection_tree.see(name)
        self._suppress_tree = False
        if load:
            self.activate_collection(name)

    def activate_collection(self, name: str) -> None:
        self.current_collection = name
        self.current_doc_id = None
        self.editing_id = None
        self.is_new_doc = False
        if self.search_var.get():
            self.search_var.set("")
        self.docs = []
        self.docs_by_id = {}
        clear_tree(self.doc_tree)
        self.clear_editor()
        self.update_editor_title()
        self.docs_info_label.configure(text=f"{name}：加载中…")
        self.set_status(f"正在加载 {name}…")
        self.run_async(
            lambda: self.session.fetch_docs(name),
            lambda docs: self.on_docs_loaded(name, docs),
            f"正在加载 {name}…",
        )

    def on_docs_loaded(self, collection: str, docs: list[dict]) -> None:
        if collection != self.current_collection:
            return
        self.docs = docs
        self.docs_by_id = {str(doc.get("_id", "")): doc for doc in docs}
        self.render_docs()
        self.set_status(f"{collection}：{len(docs)} 条文档", "success")
        if self.current_doc_id and self.current_doc_id in self.docs_by_id:
            self.select_doc(self.current_doc_id, load=True)
        elif self.is_new_doc and self.editing_id:
            # 保持新建状态，不覆盖编辑内容
            self.update_editor_title()
        elif not self.editor_dirty:
            if docs:
                self.select_doc(str(docs[0].get("_id", "")), load=True)
            else:
                self.clear_editor()

    def render_docs(self) -> None:
        if not self.current_collection:
            return
        query = self.search_var.get().strip().lower()
        rows = self.docs
        if query:
            rows = [
                doc
                for doc in self.docs
                if query in json.dumps(doc, ensure_ascii=False).lower()
            ]

        self._suppress_tree = True
        clear_tree(self.doc_tree)
        columns = self.column_plan(self.current_collection, self.docs)
        self.doc_tree["columns"] = tuple(key for key, _label, _width in columns)
        for key, label, width in columns:
            self.doc_tree.heading(key, text=label)
            self.doc_tree.column(
                key,
                width=width,
                minwidth=50,
                stretch=(key == columns[-1][0]),
                anchor="w",
            )
        for doc in rows:
            values = []
            for key, _label, _width in columns:
                if key == "_id":
                    values.append(str(doc.get("_id", ""))[:10])
                else:
                    values.append(format_cell(key, doc.get(key)))
            self.doc_tree.insert("", "end", iid=str(doc.get("_id")), values=values)
        self._suppress_tree = False

        text = f"{self.current_collection}：共 {len(self.docs)} 条"
        if query:
            text += f"，匹配 {len(rows)} 条"
        self.docs_info_label.configure(text=text)

    def column_plan(self, collection: str, docs: list[dict]) -> list[tuple[str, str, int]]:
        plan = COLUMN_PLANS.get(collection)
        if plan:
            return plan
        keys: list[str] = []
        for doc in docs:
            for key in doc:
                if key in ("_openid", "_id", "attachments") or key in keys:
                    continue
                keys.append(key)
        keys = keys[:6]
        return [("_id", "ID", 100)] + [(key, key, 150) for key in keys]

    def on_collection_select(self, _event: Optional[tk.Event] = None) -> None:
        if self._suppress_tree:
            return
        if self.busy:
            return
        selection = self.collection_tree.selection()
        if not selection:
            return
        name = selection[0]
        if name == self.current_collection:
            return
        if not self.confirm_discard():
            if self.current_collection:
                self.select_collection(self.current_collection, load=False)
            return
        self.activate_collection(name)

    def select_doc(self, doc_id: str, *, load: bool) -> None:
        if doc_id not in self.docs_by_id:
            return
        self._suppress_tree = True
        self.doc_tree.selection_set(doc_id)
        self.doc_tree.focus(doc_id)
        self.doc_tree.see(doc_id)
        self._suppress_tree = False
        if load:
            self.show_doc(doc_id, force=True)

    def on_doc_select(self, _event: Optional[tk.Event] = None) -> None:
        if self._suppress_tree:
            return
        if self.busy:
            return
        selection = self.doc_tree.selection()
        if not selection:
            return
        doc_id = selection[0]
        if doc_id == self.current_doc_id and not self.is_new_doc:
            return
        if not self.show_doc(doc_id, force=False):
            # 用户放弃切换，恢复原选择
            if self.current_doc_id:
                self.select_doc(self.current_doc_id, load=False)
            elif self.is_new_doc and self.doc_tree.selection():
                self.doc_tree.selection_remove(*self.doc_tree.selection())

    def show_doc(self, doc_id: str, *, force: bool) -> bool:
        doc = self.docs_by_id.get(doc_id)
        if not doc:
            return False
        if not force and not self.confirm_discard():
            return False
        self.current_doc_id = doc_id
        self.editing_id = doc_id
        self.is_new_doc = False
        self.set_editor_json(doc, dirty=False)
        self.update_editor_title()
        return True

    def refresh_all(self) -> None:
        if self.busy:
            return
        if not self.confirm_discard():
            return

        def work() -> tuple[dict[str, int], list[dict], Optional[str]]:
            collections = self.session.collections()
            collection = self.current_collection
            docs = self.session.fetch_docs(collection) if collection else []
            return collections, docs, collection

        self.run_async(work, self.on_refreshed, "正在刷新…")

    def on_refreshed(
        self, result: tuple[dict[str, int], list[dict], Optional[str]]
    ) -> None:
        collections, docs, collection = result
        self.apply_collections(collections)
        if collection and collection in collections:
            self.current_collection = collection
            self.on_docs_loaded(collection, docs)
            self.select_collection(collection, load=False)
        elif collections:
            target = "users" if "users" in collections else next(iter(collections))
            self.select_collection(target, load=True)
        else:
            self.clear_editor()
        self.set_status("刷新完成", "success")

    # ---- 编辑 ----
    def set_editor_json(self, doc: dict, *, dirty: bool) -> None:
        text = json.dumps(doc, ensure_ascii=False, indent=2)
        self._loading_editor = True
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text)
        self.editor.edit_modified(False)
        self._loading_editor = False
        self.editor_dirty = dirty
        self.update_editor_title()

    def clear_editor(self) -> None:
        self._loading_editor = True
        self.editor.delete("1.0", "end")
        self.editor.edit_modified(False)
        self._loading_editor = False
        self.editor_dirty = False
        self.current_doc_id = None
        self.editing_id = None
        self.is_new_doc = False
        self.update_editor_title()

    def on_editor_modified(self, _event: Optional[tk.Event] = None) -> None:
        modified = self.editor.edit_modified()
        if modified and not self._loading_editor:
            self.editor_dirty = True
            self.update_editor_title()
        self.editor.edit_modified(False)

    def update_editor_title(self) -> None:
        if self.is_new_doc and self.editing_id:
            text = f"新建文档 · {self.current_collection or ''}"
        elif self.editing_id:
            text = f"{self.current_collection or ''} / {self.editing_id}"
        else:
            text = "文档 JSON（未选择）"
        if self.editor_dirty:
            text += "  ● 未保存"
        self.editor_title.configure(text=text)

    def confirm_discard(self) -> bool:
        if not self.editor_dirty:
            return True
        return messagebox.askyesno(
            "未保存的修改",
            "当前编辑内容尚未保存，确定放弃修改吗？",
            parent=self,
        )

    def new_doc(self) -> None:
        if self.busy or not self.current_collection:
            messagebox.showinfo(APP_TITLE, "请先在左侧选择一个集合。", parent=self)
            return
        if not self.confirm_discard():
            return
        doc_id = str(uuid.uuid4())
        self.current_doc_id = None
        self.editing_id = doc_id
        self.is_new_doc = True
        self._suppress_tree = True
        selected = self.doc_tree.selection()
        if selected:
            self.doc_tree.selection_remove(*selected)
        self._suppress_tree = False
        self.set_editor_json(
            new_doc_template(self.current_collection, doc_id), dirty=False
        )
        self.editor.focus_set()

    def save_doc(self) -> None:
        if self.busy:
            return
        if not self.current_collection:
            messagebox.showinfo(APP_TITLE, "请先在左侧选择一个集合。", parent=self)
            return
        raw = self.editor.get("1.0", "end-1c").strip()
        if not raw:
            messagebox.showinfo(APP_TITLE, "编辑区内容为空。", parent=self)
            return
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            messagebox.showerror("JSON 格式错误", f"{exc}", parent=self)
            return
        if not isinstance(doc, dict):
            messagebox.showerror(APP_TITLE, "文档必须是 JSON 对象（{...}）。", parent=self)
            return

        collection = self.current_collection
        if self.is_new_doc:
            doc_id = str(doc.get("_id") or self.editing_id or uuid.uuid4())
            if doc_id in self.docs_by_id and not messagebox.askyesno(
                "ID 已存在",
                f"该集合中已存在 ID 为 {doc_id} 的文档，保存会覆盖它。\n\n是否继续？",
                parent=self,
            ):
                return
            doc["_id"] = doc_id
            if "createTime" not in doc:
                doc["createTime"] = now_ms()
            self.editing_id = doc_id
        elif self.editing_id:
            if str(doc.get("_id") or "") != self.editing_id:
                messagebox.showerror(
                    APP_TITLE, "不能修改已有文档的 _id。", parent=self
                )
                return
            doc_id = self.editing_id
        else:
            messagebox.showinfo(APP_TITLE, "请先选择文档，或点击「新增」。", parent=self)
            return

        self.run_async(
            lambda: self.session.save_doc(collection, doc),
            lambda result: self.on_doc_saved(collection, doc_id, result),
            "正在保存文档…",
        )

    def on_doc_saved(
        self, collection: str, doc_id: str, result: Any
    ) -> None:
        self.is_new_doc = False
        self.current_doc_id = doc_id
        self.editor_dirty = False
        self.reload_collection(
            collection,
            select_id=doc_id,
            success_text=f"已保存 {collection}/{doc_id}",
        )

    def reload_collection(
        self,
        collection: str,
        select_id: Optional[str] = None,
        success_text: Optional[str] = None,
    ) -> None:
        if collection != self.current_collection:
            return

        def work() -> tuple[dict[str, int], list[dict]]:
            return self.session.collections(), self.session.fetch_docs(collection)

        def done(result: tuple[dict[str, int], list[dict]]) -> None:
            collections, docs = result
            self.apply_collections(collections)
            if collection in collections:
                self.select_collection(collection, load=False)
            self.docs = docs
            self.docs_by_id = {
                str(doc.get("_id", "")): doc for doc in docs
            }
            self.render_docs()
            target = select_id or self.current_doc_id
            if target and target in self.docs_by_id:
                self.select_doc(target, load=True)
            elif self.is_new_doc and self.editing_id:
                self.update_editor_title()
            elif not self.editor_dirty:
                if docs:
                    self.select_doc(str(docs[0].get("_id", "")), load=True)
                else:
                    self.clear_editor()
            self.set_status(
                success_text or f"{collection}：{len(docs)} 条文档", "success"
            )

        self.run_async(work, done, "正在刷新文档…")

    def format_editor(self) -> None:
        raw = self.editor.get("1.0", "end-1c").strip()
        if not raw:
            return
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            messagebox.showerror("JSON 格式错误", f"{exc}", parent=self)
            return
        self._loading_editor = True
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", json.dumps(doc, ensure_ascii=False, indent=2))
        self.editor.edit_modified(False)
        self._loading_editor = False
        self.editor_dirty = True
        self.update_editor_title()

    def delete_doc(self) -> None:
        if self.busy or not self.current_collection:
            return
        if self.is_new_doc:
            if self.confirm_discard():
                self.clear_editor()
                self.set_status("已取消新建文档")
            return
        doc_id = self.current_doc_id
        if not doc_id:
            messagebox.showinfo(APP_TITLE, "请先选择要删除的文档。", parent=self)
            return
        doc = self.docs_by_id.get(doc_id, {})
        if not messagebox.askyesno(
            "删除文档",
            f"确定删除 {self.current_collection}/{doc_id} 吗？\n\n"
            f"{doc_summary(doc)}\n\n该操作不可撤销。",
            parent=self,
        ):
            return
        collection = self.current_collection
        self.run_async(
            lambda: self.session.delete_doc(collection, doc_id),
            lambda output: self.on_doc_deleted(collection, output),
            "正在删除文档…",
        )

    def on_doc_deleted(self, collection: str, output: str) -> None:
        message = (output.strip() or "已删除").splitlines()[-1]
        self.current_doc_id = None
        self.editing_id = None
        self.is_new_doc = False
        self.clear_editor()
        self.reload_collection(collection, success_text=message)

    def copy_selected_id(self) -> None:
        selection = self.doc_tree.selection()
        doc_id = selection[0] if selection else None
        if doc_id:
            self.clipboard_clear()
            self.clipboard_append(str(doc_id))
            self.set_status(f"已复制 ID：{doc_id}")

    def copy_editing_id(self) -> None:
        if self.editing_id:
            self.clipboard_clear()
            self.clipboard_append(str(self.editing_id))
            self.set_status(f"已复制 ID：{self.editing_id}")

    def show_doc_menu(self, event: tk.Event) -> None:
        row = self.doc_tree.identify_row(event.y)
        if row:
            self._suppress_tree = True
            self.doc_tree.selection_set(row)
            self._suppress_tree = False
            self.on_doc_select()
            self.doc_menu.tk_popup(event.x_root, event.y_root)

    # ---- 备份 / 状态 ----
    def backup_database(self) -> None:
        if self.busy:
            return
        path = simpledialog.askstring(
            "备份数据库",
            "远程保存路径（留空使用默认 backups/ 目录）：",
            parent=self,
        )
        if path is None:
            return
        path = path.strip()
        self.run_async(
            lambda: self.session.backup(path),
            self.on_backup_done,
            "正在备份数据库…",
        )

    def on_backup_done(self, output: str) -> None:
        self.set_status("备份完成", "success")
        messagebox.showinfo("备份完成", output.strip(), parent=self)
        match = re.search(r"已备份到\s+(.+?)（", output) or re.search(
            r"已备份到\s+(.+?)\s*$", output, re.M
        )
        if not match:
            return
        remote_path = match.group(1).strip()
        if not messagebox.askyesno(
            "下载备份", f"是否把备份下载到本地？\n\n{remote_path}", parent=self
        ):
            return
        local_path = filedialog.asksaveasfilename(
            parent=self,
            title="保存备份到本地",
            initialfile=Path(remote_path).name,
            defaultextension=".sqlite",
            filetypes=[("SQLite 数据库", "*.sqlite"), ("所有文件", "*.*")],
        )
        if not local_path:
            return
        self.run_async(
            lambda: self.session.download(remote_path, local_path),
            lambda _result: self.on_download_done(local_path),
            "正在下载备份…",
        )

    def on_download_done(self, local_path: str) -> None:
        self.set_status(f"备份已下载：{local_path}", "success")
        messagebox.showinfo("下载完成", f"已保存到：\n{local_path}", parent=self)

    def show_status(self) -> None:
        if self.busy:
            return
        self.run_async(self.session.status, self.show_text_window, "正在读取数据库状态…")

    def show_text_window(self, text: str) -> None:
        self.set_status("数据库状态已读取", "success")
        window = tk.Toplevel(self)
        window.title("数据库状态")
        window.geometry("760x560")
        window.transient(self)
        frame = ttk.Frame(window, padding=8)
        frame.pack(fill=tk.BOTH, expand=True)
        box = tk.Text(frame, wrap="none", font=("Consolas", 10), padx=8, pady=8)
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=box.yview)
        box.configure(yscrollcommand=scroll.set)
        box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        box.insert("1.0", text)
        box.configure(state="disabled")
        ttk.Button(window, text="关闭", command=window.destroy).pack(pady=(0, 8))

    # ---- SQL 窗口 ----
    def open_sql_window(self) -> None:
        if self.busy:
            return
        SqlWindow(self)

    def after_sql_change(self) -> None:
        if self.editor_dirty:
            self.set_status("SQL 已执行；当前编辑内容未保存，未自动刷新", "success")
        else:
            self.refresh_all()

    # ---- 其他 ----
    def focus_search(self) -> None:
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")

    def show_about(self) -> None:
        messagebox.showinfo(
            "关于",
            f"{APP_TITLE} v{VERSION}\n\n"
            "通过 SSH 在服务器上运行 dbterm.py。\n"
            "服务器地址配置见 remote.env。",
            parent=self,
        )

    def on_close(self) -> None:
        if self.busy and not messagebox.askyesno(
            "正在处理", "当前操作尚未完成，确定要退出吗？", parent=self
        ):
            return
        if not self.confirm_discard():
            return
        self.destroy()


# ===== SQL 窗口 =====
class SqlWindow(tk.Toplevel):
    def __init__(self, app: App) -> None:
        super().__init__(app)
        self.app = app
        self.title("执行 SQL")
        self.geometry("980x680")
        self.minsize(720, 480)
        self.transient(app)

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            outer,
            text="每次执行一条 SQL 语句。查询（SELECT / EXPLAIN / PRAGMA）直接显示结果；"
            "写操作会先确认。",
        ).pack(anchor="w", pady=(0, 6))

        input_box = ttk.Frame(outer)
        input_box.pack(fill=tk.X)
        self.input = tk.Text(
            input_box,
            height=8,
            wrap="none",
            font=("Consolas", 10),
            padx=8,
            pady=8,
            relief="solid",
            borderwidth=1,
        )
        input_y = ttk.Scrollbar(input_box, orient=tk.VERTICAL, command=self.input.yview)
        input_x = ttk.Scrollbar(input_box, orient=tk.HORIZONTAL, command=self.input.xview)
        self.input.configure(yscrollcommand=input_y.set, xscrollcommand=input_x.set)
        self.input.grid(row=0, column=0, sticky="nsew")
        input_y.grid(row=0, column=1, sticky="ns")
        input_x.grid(row=1, column=0, sticky="ew")
        input_box.rowconfigure(0, weight=1)
        input_box.columnconfigure(0, weight=1)
        self.input.insert(
            "1.0",
            "SELECT collection, COUNT(*) AS n FROM documents "
            "GROUP BY collection ORDER BY collection;",
        )

        buttons = ttk.Frame(outer)
        buttons.pack(fill=tk.X, pady=8)
        self.run_button = ttk.Button(buttons, text="执行 (Ctrl+Enter)", command=self.run)
        self.run_button.pack(side=tk.LEFT)
        ttk.Button(buttons, text="关闭", command=self.destroy).pack(side=tk.RIGHT)
        self.result_label = ttk.Label(buttons, text="")
        self.result_label.pack(side=tk.LEFT, padx=(12, 0))

        result_box = ttk.Frame(outer)
        result_box.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(result_box, show="headings")
        result_y = ttk.Scrollbar(result_box, orient=tk.VERTICAL, command=self.tree.yview)
        result_x = ttk.Scrollbar(result_box, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=result_y.set, xscrollcommand=result_x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        result_y.grid(row=0, column=1, sticky="ns")
        result_x.grid(row=1, column=0, sticky="ew")
        result_box.rowconfigure(0, weight=1)
        result_box.columnconfigure(0, weight=1)

        # 纯文本输出区（写操作/错误信息）
        self.output_text = tk.Text(
            outer,
            height=5,
            wrap="none",
            font=("Consolas", 10),
            padx=8,
            pady=8,
            relief="solid",
            borderwidth=1,
        )
        self.output_text.pack(fill=tk.X, pady=(8, 0))

        self.bind("<Control-Return>", lambda _e: self.run())
        self.input.focus_set()

    def run(self) -> None:
        if self.app.busy:
            return
        sql = self.input.get("1.0", "end-1c").strip()
        if not sql:
            return
        is_read = bool(READ_SQL_RE.match(sql))
        if not is_read and not messagebox.askyesno(
            "确认执行写操作",
            "以下 SQL 会修改数据库，且不可撤销：\n\n" + sql[:1200],
            parent=self,
        ):
            return

        self.run_button.state(["disabled"])
        self.result_label.configure(text="执行中…")
        self.tree.delete(*self.tree.get_children())
        self.output_text.delete("1.0", "end")

        if is_read:
            self.app.run_async(
                lambda: self.app.session.sql_read(sql),
                self.show_rows,
                "正在执行 SQL 查询…",
                on_error=self.show_error,
            )
        else:
            self.app.run_async(
                lambda: self.app.session.sql_write(sql),
                self.show_write_result,
                "正在执行 SQL…",
                on_error=self.show_error,
            )

    def show_error(self, exc: Exception) -> None:
        if not self._callback_guard():
            return
        self.run_button.state(["!disabled"])
        self.result_label.configure(text="执行失败")
        self.output_text.delete("1.0", "end")
        self.output_text.insert("1.0", str(exc))
        messagebox.showerror("SQL 执行失败", str(exc), parent=self)

    def _callback_guard(self) -> bool:
        return bool(self.winfo_exists())

    def show_rows(self, rows: list[dict]) -> None:
        if not self._callback_guard():
            return
        self.run_button.state(["!disabled"])
        self.result_label.configure(text=f"{len(rows)} 行")
        self.app.set_status(f"SQL 查询完成：{len(rows)} 行", "success")
        if not rows:
            self.tree["columns"] = ()
            self.output_text.insert("1.0", "查询成功，没有返回数据。")
            return
        columns: list[str] = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        self.tree["columns"] = tuple(columns)
        for key in columns:
            self.tree.heading(key, text=key)
            self.tree.column(key, width=150, minwidth=60, stretch=False, anchor="w")
        for row in rows[:1000]:
            self.tree.insert(
                "",
                "end",
                values=[format_cell(key, row.get(key)) for key in columns],
            )
        if len(rows) > 1000:
            self.output_text.insert("1.0", f"仅显示前 1000 行，共 {len(rows)} 行。")

    def show_write_result(self, output: str) -> None:
        if not self._callback_guard():
            return
        self.run_button.state(["!disabled"])
        self.result_label.configure(text="执行完成")
        self.app.set_status("SQL 执行完成", "success")
        self.output_text.insert("1.0", output.strip() or "执行完成")
        self.app.after_sql_change()


# ===== 入口 =====
def show_startup_error(message: str) -> None:
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_TITLE, message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(argv or sys.argv[1:])

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    config_path = remote.CONFIG_FILE
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--config":
            index += 1
            if index >= len(argv):
                show_startup_error("--config 缺少文件路径。")
                return 2
            config_path = Path(argv[index]).expanduser().resolve()
        elif token.startswith("--config="):
            config_path = Path(token.split("=", 1)[1]).expanduser().resolve()
        elif token in ("-h", "--help"):
            print("用法：python dbterm_gui.py [--config remote.env]")
            return 0
        elif token in ("-v", "--version"):
            print(f"dbterm-gui {VERSION}")
            return 0
        else:
            show_startup_error(f"未知参数：{token}")
            return 2
        index += 1

    try:
        config = remote.load_config(config_path)
    except remote.ConfigError as exc:
        show_startup_error(str(exc))
        return 1

    app = App(config, config_path=config_path)
    app.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        try:
            with LOG_FILE.open("a", encoding="utf-8") as handle:
                handle.write(
                    time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + traceback.format_exc() + "\n"
                )
        except OSError:
            pass
        show_startup_error("程序发生未处理的错误，详情见 dbterm_gui.log。")
        sys.exit(1)
