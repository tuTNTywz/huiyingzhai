// 业务接口：所有写操作在服务端校验权限，读操作返回与云开发一致的文档结构
const store = require("./db");
const config = require("./config");
const { pushSubscribeMessage } = require("./push");
const { deleteUploads } = require("./files");

// 学科板块白名单（与小程序端 utils/subjects.js 保持一致）
const SUBJECT_KEYS = [
  "english",
  "math",
  "psychology",
  "ai",
  "physics",
  "defense",
  "ideology",
  "pe",
  "career",
];

// 科目名称（与小程序端 utils/subjects.js 保持一致），用于推送消息备注
const SUBJECT_NAMES = {
  english: "大学英语",
  math: "高等数学",
  psychology: "大学生心理健康教育",
  ai: "人工智能基础及应用",
  physics: "大学物理",
  defense: "国防教育",
  ideology: "思想道德与法治",
  pe: "体育",
  career: "职业生涯规划",
};

// 班委或管理员均视为管理角色（可发布/删除作业、活动等）
const isManager = (u) => u.role === "committee" || u.role === "admin";

// 判断是否某科目课代表：名单 roster 集合中该学号的 repSubjects 包含该科目
function isRepOf(studentId, subjectKey) {
  if (!studentId || !subjectKey) return false;
  return !!store.findOne(
    "roster",
    (r) =>
      r.studentId === String(studentId) &&
      Array.isArray(r.repSubjects) &&
      r.repSubjects.includes(subjectKey)
  );
}

// 读取单个文档：不存在时返回 null
function getDocOrNull(collection, id) {
  return store.get(collection, id);
}

// 服务端规范化附件，限制数量并剔除无效 fileID
function normalizeAttachments(values) {
  return (Array.isArray(values) ? values : [])
    .slice(0, 5)
    .filter((a) => a && a.fileID)
    .map((a) => ({
      fileID: String(a.fileID),
      name: String(a.name || "附件"),
      type: a.type === "image" ? "image" : "file",
      size: Number(a.size) || 0,
    }));
}

// 删除文档后清理上传附件，失败不影响主流程
async function removeFiles(records) {
  const fileIDs = [];
  (records || []).forEach((r) => {
    (r.attachments || []).forEach((a) => {
      if (a && a.fileID) fileIDs.push(a.fileID);
    });
  });
  if (fileIDs.length) await deleteUploads(fileIDs);
}

const toMs = (v) => Number(v) || 0;

// 班级作业推送：作业名称栏显示「班级作业：标题」，点击进入作业详情
function pushHomeworkNotice({ homeworkId, title, deadline, content, publisherOpenid }) {
  return pushSubscribeMessage({
    page: `pages/homework/detail?id=${homeworkId}`,
    title: `班级作业：${title || ""}`,
    time: deadline,
    remark: content,
    excludeOpenid: publisherOpenid,
  });
}

// 学科任务推送：作业名称栏显示「科目名：标题」，点击进入对应学科板块
function pushSubjectTaskNotice({ subject, title, deadline, content, publisherOpenid }) {
  return pushSubscribeMessage({
    page: `pages/subject/index?key=${subject}`,
    title: `${SUBJECT_NAMES[subject] || "科目任务"}：${title || ""}`,
    time: deadline || Date.now(),
    remark: content,
    excludeOpenid: publisherOpenid,
  });
}

// 未注册用户仍可浏览读接口，写操作一律拦截
const WRITE_ACTIONS = new Set([
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
]);

async function handleAction(user, action, data = {}) {
  const openid = user._openid;

  // 部署自检：小程序端可用 { action: "ping" } 验证服务是否正常
  if (action === "ping") {
    return { ok: true, pong: true, openid, time: Date.now() };
  }

  if (action === "login") {
    return { ok: true, user, registered: !!user.registered };
  }

  if (WRITE_ACTIONS.has(action) && !user.registered) {
    return { ok: false, msg: "请先完成入斋登记" };
  }

  switch (action) {
    // ===== 学籍登记 =====
    case "register": {
      const name = String(data.name || "").trim();
      const studentId = String(data.studentId || "").trim();
      const className = String(data.className || "").trim();
      if (!name || !studentId || !className) {
        return { ok: false, msg: "请完整填写姓名、学号与班级" };
      }
      // 学号唯一，防止冒用他人学籍
      const dup = store.find(
        "users",
        (u) => u.studentId === studentId && u._openid !== openid
      );
      if (dup.length) return { ok: false, msg: "该学号已被注册" };

      // 职务以数据库 roster 集合为准：学号命中则取库内职务，否则为普通同学
      const roster = store.findOne("roster", (r) => r.studentId === studentId);
      const position = (roster && roster.position) || "普通同学";
      // 管理员/班长职务 → admin；其余班委职务 → committee
      const role =
        position === "管理员" || position === "班长"
          ? "admin"
          : position === "普通同学"
            ? "student"
            : "committee";

      const updated = store.update("users", user._id, {
        name,
        studentId,
        className,
        position,
        role,
        nickname: name,
        registered: true,
        updateTime: Date.now(),
      });
      return { ok: true, registered: true, user: updated };
    }

    case "setNickname": {
      const updated = store.update("users", user._id, {
        nickname: String(data.nickname || "").trim(),
      });
      return { ok: true, user: updated };
    }

    // ===== 名单查询（登记页 / 我的 / 学科板块共用） =====
    case "lookupRoster": {
      const studentId = String(data.studentId || "").trim();
      if (!studentId) return { ok: true, item: null };
      const item = store.findOne("roster", (r) => r.studentId === studentId);
      return { ok: true, item };
    }

    // ===== 课程设置：仅管理员统一编辑 =====
    case "saveSubjectSettings": {
      if (user.role !== "admin") return { ok: false, msg: "仅管理员可编辑课程设置" };
      const key = String(data.subject || "");
      if (!SUBJECT_KEYS.includes(key)) return { ok: false, msg: "板块不存在" };
      const payload = {
        subject: key,
        teacher: String(data.teacher || "").trim(),
        credit: String(data.credit || "").trim(),
        schedule: String(data.schedule || "").trim(),
        updaterName: user.nickname,
        updateTime: Date.now(),
      };
      const exist = store.findOne("subject_settings", (s) => s.subject === key);
      if (exist) store.update("subject_settings", exist._id, payload);
      else store.insert("subject_settings", payload);
      return { ok: true, settings: payload };
    }

    case "getSubjectSettings": {
      const key = String(data.subject || "");
      const item = store.findOne("subject_settings", (s) => s.subject === key);
      return { ok: true, item };
    }

    // 从名单 roster 集合查询该科目课代表
    case "getSubjectRep": {
      const key = String(data.subject || "");
      const item = store.findOne(
        "roster",
        (r) => Array.isArray(r.repSubjects) && r.repSubjects.includes(key)
      );
      return { ok: true, item };
    }

    // ===== 学科板块：名单指定的课代表（或班委/管理员）发布任务通知 =====
    case "publishSubjectTask": {
      if (!SUBJECT_KEYS.includes(data.subject)) return { ok: false, msg: "板块不存在" };
      const isRep = isRepOf(user.studentId, data.subject);
      if (!isRep && !isManager(user)) {
        return { ok: false, msg: "仅该科目课代表、班委或管理员可发布任务通知" };
      }
      const title = String(data.title || "").trim();
      if (!title) return { ok: false, msg: "请填写任务通知标题" };
      const attachments = normalizeAttachments(data.attachments);
      const task = store.insert("subject_tasks", {
        subject: data.subject,
        title,
        content: String(data.content || "").trim(),
        deadline: Number(data.deadline) || 0, // 时间戳(ms)，0 表示不设截止
        attachments,
        creatorOpenid: openid,
        creatorName: user.nickname,
        createTime: Date.now(),
      });
      // 生成站内通知，全体同学在通知中心可见（失败不影响发布本身）
      try {
        store.insert("notifications", {
          type: "subject_task",
          taskId: task._id,
          subject: data.subject,
          title,
          content: String(data.content || "").trim().slice(0, 100) || "新任务已发布，点击查看详情",
          deadline: Number(data.deadline) || 0,
          creatorName: user.nickname,
          publisherOpenid: openid, // 发布者本人不在通知中心展示此条
          createTime: Date.now(),
        });
      } catch (e) {
        console.error("学科任务站内通知写入失败", e.message);
      }
      // 微信服务通知：已订阅的同学收到学科任务提醒（发布者除外）
      await pushSubjectTaskNotice({
        subject: data.subject,
        title,
        deadline: Number(data.deadline) || 0,
        content: String(data.content || "").trim(),
        publisherOpenid: openid,
      });
      return { ok: true, id: task._id };
    }

    case "listSubjectTasks": {
      const key = String(data.subject || "");
      const list = store.find("subject_tasks", (t) => t.subject === key, {
        sortBy: "createTime",
        direction: "desc",
        limit: 50,
      });
      return { ok: true, list };
    }

    case "deleteSubjectTask": {
      const task = getDocOrNull("subject_tasks", data.id);
      if (!task) return { ok: false, msg: "任务通知不存在" };
      const isRep = task.creatorOpenid === openid || isRepOf(user.studentId, task.subject);
      if (!isRep && !isManager(user)) {
        return { ok: false, msg: "无权删除该任务通知" };
      }
      store.remove("subject_tasks", task._id);
      // 同步移除该任务产生的站内通知
      store.removeWhere("notifications", (n) => n.taskId === task._id);
      // 一并清理附件
      await removeFiles([task]);
      return { ok: true };
    }

    // ===== 班级作业 =====
    case "publishHomework": {
      if (!isManager(user)) return { ok: false, msg: "仅班委或管理员可发布作业" };
      if (!data.title || !data.deadline) return { ok: false, msg: "缺少标题或截止时间" };
      const attachments = normalizeAttachments(data.attachments);
      const homework = store.insert("homeworks", {
        title: data.title,
        desc: data.desc || "",
        deadline: Number(data.deadline), // 时间戳(ms)
        attachments,
        creatorOpenid: openid,
        creatorName: user.nickname,
        createTime: Date.now(),
      });
      // 通知失败不影响作业发布成功本身
      try {
        store.insert("notifications", {
          type: "homework",
          homeworkId: homework._id,
          title: String(data.title),
          content: String(data.desc || "").trim().slice(0, 100) || "新作业已发布，点击查看详情",
          deadline: Number(data.deadline),
          creatorName: user.nickname,
          publisherOpenid: openid, // 发布者本人不在通知中心展示此条
          createTime: Date.now(),
        });
        await pushHomeworkNotice({
          homeworkId: homework._id,
          title: data.title,
          deadline: Number(data.deadline),
          content: data.desc,
          publisherOpenid: openid,
        });
      } catch (e) {
        console.error("作业通知失败", e.message);
      }
      return { ok: true, id: homework._id };
    }

    case "listHomeworks": {
      const list = store.find("homeworks", null, {
        sortBy: "createTime",
        direction: "desc",
        limit: 20,
      });
      return { ok: true, list };
    }

    case "getHomework": {
      const item = getDocOrNull("homeworks", data.id);
      if (!item) return { ok: false, msg: "作业不存在或已被删除" };
      return { ok: true, item };
    }

    case "deleteHomework": {
      const homework = getDocOrNull("homeworks", data.id);
      if (!homework) return { ok: false, msg: "作业不存在" };
      if (!isManager(user)) return { ok: false, msg: "仅班委或管理员可删除作业" };
      store.remove("homeworks", homework._id);
      // 同步移除该作业产生的通知
      store.removeWhere("notifications", (n) => n.homeworkId === homework._id);
      // 删除附件
      await removeFiles([homework]);
      return { ok: true };
    }

    // 同学点击「开启作业提醒」授权后，登记一次可推送次数
    case "subscribeHomework": {
      if (!config.homeworkTmplId) return { ok: false, msg: "尚未配置订阅消息模板 ID" };
      const exist = store.findOne("subscriptions", (s) => s._openid === openid);
      if (exist) {
        store.update("subscriptions", exist._id, {
          remain: Number(exist.remain || 0) + 1,
          updateTime: Date.now(),
        });
      } else {
        store.insert("subscriptions", {
          _openid: openid,
          remain: 1,
          createTime: Date.now(),
        });
      }
      return { ok: true };
    }

    // 关闭作业提醒：剩余推送次数清零（微信侧授权无法撤销，仅服务端不再推送）
    case "unsubscribeHomework": {
      const exist = store.findOne("subscriptions", (s) => s._openid === openid);
      if (exist) {
        store.update("subscriptions", exist._id, { remain: 0, updateTime: Date.now() });
      }
      return { ok: true };
    }

    // 通知中心：站内通知统一由服务端返回
    case "fetchHomeworkNotices": {
      const list = store
        .find("notifications", null, { sortBy: "createTime", direction: "desc", limit: 50 })
        .map((n) => ({
          ...n,
          createMs: toMs(n.createTime),
          deadlineMs: toMs(n.deadline),
        }));
      const sub = store.findOne("subscriptions", (s) => s._openid === openid);
      return { ok: true, list, pushRemain: sub ? Number(sub.remain) || 0 : 0 };
    }

    // ===== 活动栏目：班委或管理员发布 =====
    case "publishActivity": {
      if (!isManager(user)) return { ok: false, msg: "仅班委或管理员可发布活动" };
      if (!data.title || !data.activityTime) {
        return { ok: false, msg: "缺少活动标题或时间" };
      }
      const activity = store.insert("activities", {
        title: data.title,
        place: data.place || "待定",
        content: data.content || "",
        activityTime: Number(data.activityTime), // 时间戳(ms)
        creatorOpenid: openid,
        creatorName: user.nickname,
        createTime: Date.now(),
      });
      return { ok: true, id: activity._id };
    }

    case "listActivities": {
      const list = store.find("activities", null, {
        sortBy: "activityTime",
        direction: "desc",
        limit: 20,
      });
      return { ok: true, list };
    }

    case "getActivity": {
      const item = getDocOrNull("activities", data.id);
      if (!item) return { ok: false, msg: "活动不存在或已被删除" };
      return { ok: true, item };
    }

    case "deleteActivity": {
      if (!isManager(user)) return { ok: false, msg: "仅班委或管理员可删除活动" };
      const activity = getDocOrNull("activities", data.id);
      if (!activity) return { ok: false, msg: "活动不存在或已被删除" };
      store.remove("activities", activity._id);
      return { ok: true };
    }

    // ===== 交流栏目：所有人可自由发帖、回复（可带文件 / 照片） =====
    case "createPost": {
      const content = String(data.content || "").trim();
      const attachments = normalizeAttachments(data.attachments);
      if (!content && !attachments.length) return { ok: false, msg: "请填写内容或添加附件" };
      if (content.length > 1000) return { ok: false, msg: "内容过长" };
      const post = store.insert("posts", {
        content,
        attachments,
        _openid: openid,
        nickname: user.nickname,
        role: user.role,
        replyCount: 0,
        createTime: Date.now(),
      });
      return { ok: true, id: post._id };
    }

    case "listPosts": {
      const list = store.find("posts", null, {
        sortBy: "createTime",
        direction: "desc",
        limit: 20,
      });
      return { ok: true, list };
    }

    case "getPost": {
      const item = getDocOrNull("posts", data.id);
      if (!item) return { ok: false, msg: "帖子不存在或已被删除" };
      return { ok: true, item };
    }

    case "listReplies": {
      const list = store.find("post_replies", (r) => r.postId === data.postId, {
        sortBy: "createTime",
        direction: "asc",
        limit: 100,
      });
      return { ok: true, list };
    }

    case "createReply": {
      const content = String(data.content || "").trim();
      const attachments = normalizeAttachments(data.attachments);
      if (!content && !attachments.length) return { ok: false, msg: "请填写回复或添加附件" };
      if (content.length > 500) return { ok: false, msg: "回复过长" };
      const post = getDocOrNull("posts", data.postId);
      if (!post) return { ok: false, msg: "帖子不存在" };
      store.insert("post_replies", {
        postId: data.postId,
        content,
        attachments,
        _openid: openid,
        nickname: user.nickname,
        role: user.role,
        createTime: Date.now(),
      });
      store.update("posts", post._id, {
        replyCount: Number(post.replyCount || 0) + 1,
      });
      return { ok: true };
    }

    case "deletePost": {
      const post = getDocOrNull("posts", data.id);
      if (!post) return { ok: false, msg: "帖子不存在" };
      if (post._openid !== openid && !isManager(user)) {
        return { ok: false, msg: "只能删除自己的帖子" };
      }
      const replies = store.find("post_replies", (r) => r.postId === post._id, { limit: 1000 });
      store.remove("posts", post._id);
      store.removeWhere("post_replies", (r) => r.postId === post._id);
      // 帖子与全部回复的附件一并清理
      await removeFiles([post, ...replies]);
      return { ok: true };
    }

    default:
      return { ok: false, msg: "未知操作" };
  }
}

module.exports = { handleAction, SUBJECT_KEYS };
