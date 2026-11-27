# 业务接口：所有写操作在服务端校验权限，读操作返回与云开发一致的文档结构
from __future__ import annotations

import time
from typing import Any, Optional

from . import db as store
from . import robot
from .files import delete_uploads

# 学科板块白名单（与网页版保持一致）
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

# 科目名称（与网页版保持一致）
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


NOTICE_TTL_MS = 30 * 24 * 60 * 60 * 1000


async def cleanup_expired_notices() -> int:
    """删除超过 30 天的通知，并清理其附件。"""
    cutoff = _now_ms() - NOTICE_TTL_MS
    expired = store.find(
        "notifications", lambda n: _int(n.get("createTime")) < cutoff
    )
    if not expired:
        return 0
    await remove_files(expired)
    store.remove_where(
        "notifications", lambda n: _int(n.get("createTime")) < cutoff
    )
    return len(expired)


def _to_ms(value: Any) -> int:
    return _int(value)


# 作业完成状态：按学号（openid）独立记录，互不影响
def homework_done(openid: Any, homework_id: Any) -> bool:
    if not openid or not homework_id:
        return False
    doc = store.find_one(
        "homework_status",
        lambda s: s.get("homeworkId") == str(homework_id)
        and s.get("openid") == openid,
    )
    return bool(doc and doc.get("done"))


# 未注册用户仍可浏览读接口，写操作一律拦截
WRITE_ACTIONS = {
    "publishSubjectTask",
    "deleteSubjectTask",
    "publishHomework",
    "deleteHomework",
    "setHomeworkStatus",
    "publishActivity",
    "publishVolunteer",
    "deleteActivity",
    "publishNotice",
    "deleteNotice",
    "createPost",
    "createReply",
}


async def handle_action(user: dict, action: str, data: Optional[dict] = None) -> dict:
    data = data if isinstance(data, dict) else {}
    openid = user.get("_openid")

    if action in WRITE_ACTIONS and not user.get("registered"):
        return {"ok": False, "msg": "请先登录"}

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

        robot.notify_subject_task(
            subject_name=SUBJECT_NAMES.get(subject, subject),
            title=title,
            content=content,
            deadline=deadline,
            creator_name=user.get("nickname"),
            attachments=task.get("attachments"),
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

        robot.notify_homework(
            title=title,
            desc=desc,
            deadline=deadline,
            creator_name=user.get("nickname"),
            attachments=homework.get("attachments"),
        )
        return {"ok": True, "id": homework["_id"]}

    if action == "listHomeworks":
        rows = store.find("homeworks", sort_by="createTime", direction="desc", limit=20)
        for row in rows:
            row["done"] = homework_done(openid, row.get("_id"))
        return {"ok": True, "list": rows}

    if action == "getHomework":
        item = get_doc_or_none("homeworks", data.get("id"))
        if not item:
            return {"ok": False, "msg": "作业不存在或已被删除"}
        item["done"] = homework_done(openid, item.get("_id"))
        return {"ok": True, "item": item}

    # 每位同学自行切换某份作业的「已完成 / 未完成」，只影响本人
    if action == "setHomeworkStatus":
        homework = get_doc_or_none("homeworks", data.get("id"))
        if not homework:
            return {"ok": False, "msg": "作业不存在或已被删除"}
        if not openid:
            return {"ok": False, "msg": "请先登录"}
        done = bool(data.get("done"))
        existing = store.find_one(
            "homework_status",
            lambda s: s.get("homeworkId") == homework.get("_id")
            and s.get("openid") == openid,
        )
        if existing:
            store.update(
                "homework_status",
                existing.get("_id"),
                {"done": done, "updateTime": _now_ms()},
            )
        else:
            store.insert(
                "homework_status",
                {
                    "homeworkId": homework.get("_id"),
                    "openid": openid,
                    "done": done,
                    "updateTime": _now_ms(),
                },
            )
        return {"ok": True, "done": done}

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
        # 以及同学们标记的完成状态
        store.remove_where(
            "homework_status", lambda s: s.get("homeworkId") == homework.get("_id")
        )
        # 删除附件
        await remove_files([homework])
        return {"ok": True}

    # ===== 班级通知：班委/管理员手动发布 =====
    if action == "publishNotice":
        if not is_manager(user):
            return {"ok": False, "msg": "仅班委或管理员可发布通知"}
        title = _trim(data.get("title"))
        if not title:
            return {"ok": False, "msg": "请填写通知标题"}
        content = _trim(data.get("content"))
        attachments = normalize_attachments(data.get("attachments"))
        notice = store.insert(
            "notifications",
            {
                "type": "notice",
                "title": title,
                "content": content,
                "attachments": attachments,
                "creatorName": user.get("nickname"),
                "publisherOpenid": openid,
                "createTime": _now_ms(),
            },
        )
        robot.notify_notice(
            title=title,
            content=content,
            creator_name=user.get("nickname"),
            attachments=attachments,
        )
        return {"ok": True, "id": notice["_id"]}

    if action == "deleteNotice":
        notice = get_doc_or_none("notifications", data.get("id"))
        if not notice or notice.get("type") != "notice":
            return {"ok": False, "msg": "通知不存在或已被删除"}
        if not is_manager(user):
            return {"ok": False, "msg": "仅班委或管理员可删除通知"}
        store.remove("notifications", notice.get("_id"))
        await remove_files([notice])
        return {"ok": True}

    # 通知中心：站内通知统一由服务端返回
    if action == "fetchHomeworkNotices":
        await cleanup_expired_notices()
        rows = store.find(
            "notifications",
            lambda n: n.get("type") == "notice",
            sort_by="createTime",
            direction="desc",
        )
        notices = [
            {
                **n,
                "createMs": _to_ms(n.get("createTime")),
                "fullContent": n.get("content") or "",
                "attachments": n.get("attachments") or [],
            }
            for n in rows
        ]
        return {"ok": True, "list": notices}

    # ===== 活动栏目：班委或管理员发布 =====
    if action == "publishActivity":
        if not is_manager(user):
            return {"ok": False, "msg": "仅班委或管理员可发布活动"}
        if not data.get("title") or not data.get("activityTime"):
            return {"ok": False, "msg": "缺少活动标题或时间"}

        activity = store.insert(
            "activities",
            {
                "category": "activity",
                "title": data.get("title"),
                "place": data.get("place") or "待定",
                "content": data.get("content") or "",
                "activityTime": _int(data.get("activityTime")),  # 时间戳(ms)
                "creatorOpenid": openid,
                "creatorName": user.get("nickname"),
                "createTime": _now_ms(),
            },
        )
        robot.notify_activity(
            title=activity.get("title"),
            place=activity.get("place"),
            activity_time=activity.get("activityTime"),
            content=activity.get("content"),
            creator_name=activity.get("creatorName"),
        )
        return {"ok": True, "id": activity["_id"]}

    if action == "publishVolunteer":
        if not is_manager(user):
            return {"ok": False, "msg": "仅班委或管理员可发布志愿活动"}
        if not data.get("title") or not data.get("activityTime"):
            return {"ok": False, "msg": "缺少志愿活动标题或时间"}
        start_time = _int(data.get("activityTime"))
        end_time = _int(data.get("endTime"))
        if end_time and end_time < start_time:
            return {"ok": False, "msg": "结束时间不能早于开始时间"}
        signup = _trim(data.get("signupUrl"))
        if signup and not (signup.startswith("http://") or signup.startswith("https://")):
            signup = ""

        volunteer = store.insert(
            "activities",
            {
                "category": "volunteer",
                "title": _trim(data.get("title")),
                "place": _trim(data.get("place")),
                "content": _trim(data.get("content")),
                "activityTime": start_time,  # 开始时间，时间戳(ms)
                "endTime": end_time,  # 结束时间，0 表示未填写
                "quota": _trim(data.get("quota")),
                "organizer": _trim(data.get("organizer")),
                "contact": _trim(data.get("contact")),
                "signupUrl": signup,
                "creatorOpenid": openid,
                "creatorName": user.get("nickname"),
                "createTime": _now_ms(),
            },
        )
        robot.notify_volunteer(
            title=volunteer.get("title"),
            place=volunteer.get("place"),
            activity_time=volunteer.get("activityTime"),
            end_time=volunteer.get("endTime"),
            content=volunteer.get("content"),
            creator_name=volunteer.get("creatorName"),
            quota=volunteer.get("quota"),
            organizer=volunteer.get("organizer"),
            contact=volunteer.get("contact"),
            signup_url=volunteer.get("signupUrl"),
        )
        return {"ok": True, "id": volunteer["_id"]}

    if action == "listVolunteers":
        rows = store.find(
            "activities",
            lambda a: a.get("category") == "volunteer",
            sort_by="activityTime",
            direction="desc",
            limit=20,
        )
        return {"ok": True, "list": rows}

    if action == "listActivities":
        rows = store.find(
            "activities",
            lambda a: a.get("category") != "volunteer",
            sort_by="activityTime",
            direction="desc",
            limit=20,
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
        store.remove_where("notifications", lambda n: n.get("activityId") == activity.get("_id"))
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

    return {"ok": False, "msg": "未知操作"}
