# 业务接口：所有写操作在服务端校验权限，读操作返回与云开发一致的文档结构
from __future__ import annotations

import time
from typing import Any, Optional

from . import db as store
from .config import config
from .files import delete_uploads
from .push import push_subscribe_message

# 学科板块白名单（与小程序端 utils/subjects.js 保持一致）
SUBJECT_KEYS = [
    "english",
    "math",
    "psychology",
    "ai",
    "physics",
    "defense",
    "ideology",
    "pe",
    "career",
]

# 科目名称（与小程序端 utils/subjects.js 保持一致），用于推送消息备注
SUBJECT_NAMES = {
    "english": "大学英语",
    "math": "高等数学",
    "psychology": "大学生心理健康教育",
    "ai": "人工智能基础及应用",
    "physics": "大学物理",
    "defense": "国防教育",
    "ideology": "思想道德与法治",
    "pe": "体育",
    "career": "职业生涯规划",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def _trim(value: Any) -> str:
    return _str(value).strip()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# 班委或管理员均视为管理角色（可发布/删除作业、活动等）
def is_manager(user: dict) -> bool:
    return user.get("role") in ("committee", "admin")


# 判断是否某科目课代表：名单 roster 集合中该学号的 repSubjects 包含该科目
def is_rep_of(student_id: Any, subject_key: Any) -> bool:
    if not student_id or not subject_key:
        return False
    target = str(student_id)
    return bool(
        store.find_one(
            "roster",
            lambda r: r.get("studentId") == target
            and isinstance(r.get("repSubjects"), list)
            and subject_key in r["repSubjects"],
        )
    )


# 读取单个文档：不存在时返回 None
def get_doc_or_none(collection: str, doc_id: Any) -> Optional[dict]:
    return store.get(collection, doc_id)


# 服务端规范化附件，限制数量并剔除无效 fileID
def normalize_attachments(values: Any) -> list[dict]:
    items = values if isinstance(values, list) else []
    result = []
    for item in items[:5]:
        if not isinstance(item, dict) or not item.get("fileID"):
            continue
        result.append(
            {
                "fileID": str(item["fileID"]),
                "name": str(item.get("name") or "附件"),
                "type": "image" if item.get("type") == "image" else "file",
                "size": _int(item.get("size")),
            }
        )
    return result


# 删除文档后清理上传附件，失败不影响主流程
async def remove_files(records: Any) -> None:
    file_ids = []
    for record in records or []:
        for attachment in record.get("attachments") or []:
            if isinstance(attachment, dict) and attachment.get("fileID"):
                file_ids.append(attachment["fileID"])
    if file_ids:
        await delete_uploads(file_ids)


def _to_ms(value: Any) -> int:
    return _int(value)


# 班级作业推送：作业名称栏显示「班级作业：标题」，点击进入作业详情
async def push_homework_notice(
    *, homework_id: str, title: str, deadline: Any, content: Any, publisher_openid: str
) -> None:
    await push_subscribe_message(
        page=f"pages/homework/detail?id={homework_id}",
        title=f"班级作业：{title or ''}",
        time=deadline,
        remark=content,
        exclude_openid=publisher_openid,
    )


# 学科任务推送：作业名称栏显示「科目名：标题」，点击进入对应学科板块
async def push_subject_task_notice(
    *, subject: str, title: str, deadline: Any, content: Any, publisher_openid: str
) -> None:
    await push_subscribe_message(
        page=f"pages/subject/index?key={subject}",
        title=f"{SUBJECT_NAMES.get(subject, '科目任务')}：{title or ''}",
        time=deadline or _now_ms(),
        remark=content,
        exclude_openid=publisher_openid,
    )


# 未注册用户仍可浏览读接口，写操作一律拦截
WRITE_ACTIONS = {
    "setNickname",
    "saveSubjectSettings",
    "publishSubjectTask",
    "deleteSubjectTask",
    "publishHomework",
    "deleteHomework",
    "subscribeHomework",
    "unsubscribeHomework",
    "publishActivity",
    "deleteActivity",
    "createPost",
    "createReply",
    "deletePost",
}


async def handle_action(user: dict, action: str, data: Optional[dict] = None) -> dict:
    data = data if isinstance(data, dict) else {}
    openid = user.get("_openid")

    # 部署自检：小程序端可用 { action: "ping" } 验证服务是否正常
    if action == "ping":
        return {"ok": True, "pong": True, "openid": openid, "time": _now_ms()}

    if action == "login":
        return {"ok": True, "user": user, "registered": bool(user.get("registered"))}

    if action in WRITE_ACTIONS and not user.get("registered"):
        return {"ok": False, "msg": "请先完成入斋登记"}

    # ===== 学籍登记 =====
    if action == "register":
        name = _trim(data.get("name"))
        student_id = _trim(data.get("studentId"))
        class_name = _trim(data.get("className"))
        if not name or not student_id or not class_name:
            return {"ok": False, "msg": "请完整填写姓名、学号与班级"}

        # 学号唯一，防止冒用他人学籍
        duplicate = store.find(
            "users",
            lambda u: u.get("studentId") == student_id and u.get("_openid") != openid,
        )
        if duplicate:
            return {"ok": False, "msg": "该学号已被注册"}

        # 职务以数据库 roster 集合为准：学号命中则取库内职务，否则为普通同学
        roster = store.find_one("roster", lambda r: r.get("studentId") == student_id)
        position = (roster or {}).get("position") or "普通同学"
        # 管理员/班长职务 → admin；其余班委职务 → committee
        if position in ("管理员", "班长"):
            role = "admin"
        elif position == "普通同学":
            role = "student"
        else:
            role = "committee"

        updated = store.update(
            "users",
            user.get("_id"),
            {
                "name": name,
                "studentId": student_id,
                "className": class_name,
                "position": position,
                "role": role,
                "nickname": name,
                "registered": True,
                "updateTime": _now_ms(),
            },
        )
        return {"ok": True, "registered": True, "user": updated}

    if action == "setNickname":
        updated = store.update(
            "users", user.get("_id"), {"nickname": _trim(data.get("nickname"))}
        )
        return {"ok": True, "user": updated}

    # ===== 名单查询（登记页 / 我的 / 学科板块共用） =====
    if action == "lookupRoster":
        student_id = _trim(data.get("studentId"))
        if not student_id:
            return {"ok": True, "item": None}
        item = store.find_one("roster", lambda r: r.get("studentId") == student_id)
        return {"ok": True, "item": item}

    # ===== 课程设置：仅管理员统一编辑 =====
    if action == "saveSubjectSettings":
        if user.get("role") != "admin":
            return {"ok": False, "msg": "仅管理员可编辑课程设置"}
        key = _str(data.get("subject"))
        if key not in SUBJECT_KEYS:
            return {"ok": False, "msg": "板块不存在"}
        payload = {
            "subject": key,
            "teacher": _trim(data.get("teacher")),
            "credit": _trim(data.get("credit")),
            "schedule": _trim(data.get("schedule")),
            "updaterName": user.get("nickname"),
            "updateTime": _now_ms(),
        }
        exist = store.find_one("subject_settings", lambda s: s.get("subject") == key)
        if exist:
            store.update("subject_settings", exist.get("_id"), payload)
        else:
            store.insert("subject_settings", payload)
        return {"ok": True, "settings": payload}

    if action == "getSubjectSettings":
        key = _str(data.get("subject"))
        item = store.find_one("subject_settings", lambda s: s.get("subject") == key)
        return {"ok": True, "item": item}

    # 从名单 roster 集合查询该科目课代表
    if action == "getSubjectRep":
        key = _str(data.get("subject"))
        item = store.find_one(
            "roster",
            lambda r: isinstance(r.get("repSubjects"), list)
            and key in r["repSubjects"],
        )
        return {"ok": True, "item": item}

    # ===== 学科板块：名单指定的课代表（或班委/管理员）发布任务通知 =====
    if action == "publishSubjectTask":
        subject = data.get("subject")
        if subject not in SUBJECT_KEYS:
            return {"ok": False, "msg": "板块不存在"}
        if not is_rep_of(user.get("studentId"), subject) and not is_manager(user):
            return {"ok": False, "msg": "仅该科目课代表、班委或管理员可发布任务通知"}

        title = _trim(data.get("title"))
        if not title:
            return {"ok": False, "msg": "请填写任务通知标题"}
        attachments = normalize_attachments(data.get("attachments"))
        content = _trim(data.get("content"))
        deadline = _int(data.get("deadline"))

        task = store.insert(
            "subject_tasks",
            {
                "subject": subject,
                "title": title,
                "content": content,
                "deadline": deadline,  # 时间戳(ms)，0 表示不设截止
                "attachments": attachments,
                "creatorOpenid": openid,
                "creatorName": user.get("nickname"),
                "createTime": _now_ms(),
            },
        )

        # 生成站内通知，全体同学在通知中心可见（失败不影响发布本身）
        try:
            store.insert(
                "notifications",
                {
                    "type": "subject_task",
                    "taskId": task["_id"],
                    "subject": subject,
                    "title": title,
                    "content": content[:100] or "新任务已发布，点击查看详情",
                    "deadline": deadline,
                    "creatorName": user.get("nickname"),
                    "publisherOpenid": openid,  # 发布者本人不在通知中心展示此条
                    "createTime": _now_ms(),
                },
            )
        except Exception as exc:
            print("学科任务站内通知写入失败", exc)

        # 微信服务通知：已订阅的同学收到学科任务提醒（发布者除外）
        await push_subject_task_notice(
            subject=subject,
            title=title,
            deadline=deadline,
            content=content,
            publisher_openid=openid,
        )
        return {"ok": True, "id": task["_id"]}

    if action == "listSubjectTasks":
        key = _str(data.get("subject"))
        rows = store.find(
            "subject_tasks",
            lambda t: t.get("subject") == key,
            sort_by="createTime",
            direction="desc",
            limit=50,
        )
        return {"ok": True, "list": rows}

    if action == "deleteSubjectTask":
        task = get_doc_or_none("subject_tasks", data.get("id"))
        if not task:
            return {"ok": False, "msg": "任务通知不存在"}
        allowed = task.get("creatorOpenid") == openid or is_rep_of(
            user.get("studentId"), task.get("subject")
        )
        if not allowed and not is_manager(user):
            return {"ok": False, "msg": "无权删除该任务通知"}

        store.remove("subject_tasks", task.get("_id"))
        # 同步移除该任务产生的站内通知
        store.remove_where("notifications", lambda n: n.get("taskId") == task.get("_id"))
        # 一并清理附件
        await remove_files([task])
        return {"ok": True}

    # ===== 班级作业 =====
    if action == "publishHomework":
        if not is_manager(user):
            return {"ok": False, "msg": "仅班委或管理员可发布作业"}
        if not data.get("title") or not data.get("deadline"):
            return {"ok": False, "msg": "缺少标题或截止时间"}

        attachments = normalize_attachments(data.get("attachments"))
        title = str(data.get("title"))
        desc = data.get("desc") or ""
        deadline = _int(data.get("deadline"))

        homework = store.insert(
            "homeworks",
            {
                "title": title,
                "desc": desc,
                "deadline": deadline,  # 时间戳(ms)
                "attachments": attachments,
                "creatorOpenid": openid,
                "creatorName": user.get("nickname"),
                "createTime": _now_ms(),
            },
        )

        # 通知失败不影响作业发布成功本身
        try:
            store.insert(
                "notifications",
                {
                    "type": "homework",
                    "homeworkId": homework["_id"],
                    "title": title,
                    "content": _trim(desc)[:100] or "新作业已发布，点击查看详情",
                    "deadline": deadline,
                    "creatorName": user.get("nickname"),
                    "publisherOpenid": openid,  # 发布者本人不在通知中心展示此条
                    "createTime": _now_ms(),
                },
            )
            await push_homework_notice(
                homework_id=homework["_id"],
                title=title,
                deadline=deadline,
                content=desc,
                publisher_openid=openid,
            )
        except Exception as exc:
            print("作业通知失败", exc)
        return {"ok": True, "id": homework["_id"]}

    if action == "listHomeworks":
        rows = store.find("homeworks", sort_by="createTime", direction="desc", limit=20)
        return {"ok": True, "list": rows}

    if action == "getHomework":
        item = get_doc_or_none("homeworks", data.get("id"))
        if not item:
            return {"ok": False, "msg": "作业不存在或已被删除"}
        return {"ok": True, "item": item}

    if action == "deleteHomework":
        homework = get_doc_or_none("homeworks", data.get("id"))
        if not homework:
            return {"ok": False, "msg": "作业不存在"}
        if not is_manager(user):
            return {"ok": False, "msg": "仅班委或管理员可删除作业"}

        store.remove("homeworks", homework.get("_id"))
        # 同步移除该作业产生的通知
        store.remove_where(
            "notifications", lambda n: n.get("homeworkId") == homework.get("_id")
        )
        # 删除附件
        await remove_files([homework])
        return {"ok": True}

    # 同学点击「开启作业提醒」授权后，登记一次可推送次数
    if action == "subscribeHomework":
        if not config.homework_tmpl_id:
            return {"ok": False, "msg": "尚未配置订阅消息模板 ID"}
        exist = store.find_one("subscriptions", lambda s: s.get("_openid") == openid)
        if exist:
            store.update(
                "subscriptions",
                exist.get("_id"),
                {
                    "remain": _int(exist.get("remain")) + 1,
                    "updateTime": _now_ms(),
                },
            )
        else:
            store.insert(
                "subscriptions",
                {"_openid": openid, "remain": 1, "createTime": _now_ms()},
            )
        return {"ok": True}

    # 关闭作业提醒：剩余推送次数清零（微信侧授权无法撤销，仅服务端不再推送）
    if action == "unsubscribeHomework":
        exist = store.find_one("subscriptions", lambda s: s.get("_openid") == openid)
        if exist:
            store.update(
                "subscriptions",
                exist.get("_id"),
                {"remain": 0, "updateTime": _now_ms()},
            )
        return {"ok": True}

    # 通知中心：站内通知统一由服务端返回
    if action == "fetchHomeworkNotices":
        rows = store.find(
            "notifications", sort_by="createTime", direction="desc", limit=50
        )
        notices = [
            {
                **n,
                "createMs": _to_ms(n.get("createTime")),
                "deadlineMs": _to_ms(n.get("deadline")),
            }
            for n in rows
        ]
        sub = store.find_one("subscriptions", lambda s: s.get("_openid") == openid)
        return {
            "ok": True,
            "list": notices,
            "pushRemain": _int(sub.get("remain")) if sub else 0,
        }

    # ===== 活动栏目：班委或管理员发布 =====
    if action == "publishActivity":
        if not is_manager(user):
            return {"ok": False, "msg": "仅班委或管理员可发布活动"}
        if not data.get("title") or not data.get("activityTime"):
            return {"ok": False, "msg": "缺少活动标题或时间"}

        activity = store.insert(
            "activities",
            {
                "title": data.get("title"),
                "place": data.get("place") or "待定",
                "content": data.get("content") or "",
                "activityTime": _int(data.get("activityTime")),  # 时间戳(ms)
                "creatorOpenid": openid,
                "creatorName": user.get("nickname"),
                "createTime": _now_ms(),
            },
        )
        return {"ok": True, "id": activity["_id"]}

    if action == "listActivities":
        rows = store.find(
            "activities", sort_by="activityTime", direction="desc", limit=20
        )
        return {"ok": True, "list": rows}

    if action == "getActivity":
        item = get_doc_or_none("activities", data.get("id"))
        if not item:
            return {"ok": False, "msg": "活动不存在或已被删除"}
        return {"ok": True, "item": item}

    if action == "deleteActivity":
        if not is_manager(user):
            return {"ok": False, "msg": "仅班委或管理员可删除活动"}
        activity = get_doc_or_none("activities", data.get("id"))
        if not activity:
            return {"ok": False, "msg": "活动不存在或已被删除"}
        store.remove("activities", activity.get("_id"))
        return {"ok": True}

    # ===== 交流栏目：所有人可自由发帖、回复（可带文件 / 照片） =====
    if action == "createPost":
        content = _trim(data.get("content"))
        attachments = normalize_attachments(data.get("attachments"))
        if not content and not attachments:
            return {"ok": False, "msg": "请填写内容或添加附件"}
        if len(content) > 1000:
            return {"ok": False, "msg": "内容过长"}

        post = store.insert(
            "posts",
            {
                "content": content,
                "attachments": attachments,
                "_openid": openid,
                "nickname": user.get("nickname"),
                "role": user.get("role"),
                "replyCount": 0,
                "createTime": _now_ms(),
            },
        )
        return {"ok": True, "id": post["_id"]}

    if action == "listPosts":
        rows = store.find("posts", sort_by="createTime", direction="desc", limit=20)
        return {"ok": True, "list": rows}

    if action == "getPost":
        item = get_doc_or_none("posts", data.get("id"))
        if not item:
            return {"ok": False, "msg": "帖子不存在或已被删除"}
        return {"ok": True, "item": item}

    if action == "listReplies":
        rows = store.find(
            "post_replies",
            lambda r: r.get("postId") == data.get("postId"),
            sort_by="createTime",
            direction="asc",
            limit=100,
        )
        return {"ok": True, "list": rows}

    if action == "createReply":
        content = _trim(data.get("content"))
        attachments = normalize_attachments(data.get("attachments"))
        if not content and not attachments:
            return {"ok": False, "msg": "请填写回复或添加附件"}
        if len(content) > 500:
            return {"ok": False, "msg": "回复过长"}
        post = get_doc_or_none("posts", data.get("postId"))
        if not post:
            return {"ok": False, "msg": "帖子不存在"}

        store.insert(
            "post_replies",
            {
                "postId": data.get("postId"),
                "content": content,
                "attachments": attachments,
                "_openid": openid,
                "nickname": user.get("nickname"),
                "role": user.get("role"),
                "createTime": _now_ms(),
            },
        )
        store.update(
            "posts",
            post.get("_id"),
            {"replyCount": _int(post.get("replyCount")) + 1},
        )
        return {"ok": True}

    if action == "deletePost":
        post = get_doc_or_none("posts", data.get("id"))
        if not post:
            return {"ok": False, "msg": "帖子不存在"}
        if post.get("_openid") != openid and not is_manager(user):
            return {"ok": False, "msg": "只能删除自己的帖子"}

        replies = store.find(
            "post_replies", lambda r: r.get("postId") == post.get("_id"), limit=1000
        )
        store.remove("posts", post.get("_id"))
        store.remove_where(
            "post_replies", lambda r: r.get("postId") == post.get("_id")
        )
        # 帖子与全部回复的附件一并清理
        await remove_files([post, *replies])
        return {"ok": True}

    return {"ok": False, "msg": "未知操作"}
