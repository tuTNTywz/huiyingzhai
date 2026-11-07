// 云函数 api：统一处理写操作，服务端校验权限
const cloud = require("wx-server-sdk");
cloud.init({ env: cloud.DYNAMIC_CURRENT_ENV });
const db = cloud.database();
const _ = db.command;

// 职务与角色以数据库 roster 集合（学号名单）为准：命中取库内职务，否则为普通同学

// 班委或管理员均视为管理角色（可发布/删除作业、活动等）
const isManager = (u) => u.role === "committee" || u.role === "admin";

// 判断是否某科目课代表：名单 roster 集合中该学号的 repSubjects 包含该科目
async function isRepOf(studentId, subjectKey) {
  if (!studentId || !subjectKey) return false;
  const res = await db
    .collection("roster")
    .where({ studentId, repSubjects: subjectKey })
    .get();
  return res.data.length > 0;
}

// 学科板块白名单（与小程序端 utils/subjects.js 保持一致）
const SUBJECT_KEYS = [
  "english", "math", "psychology", "ai", "physics",
  "defense", "ideology", "pe", "career",
];

// 业务用到的全部集合：冷启动时自动创建，避免「集合不存在」导致登录/写入失败
const COLLECTIONS = [
  "users",
  "homeworks",
  "sign_tasks",
  "sign_records",
  "activities",
  "posts",
  "post_replies",
  "subject_settings",
  "subject_tasks",
];
let collectionsReady = null;

function ensureCollections() {
  if (!collectionsReady) {
    // 并行创建以缩短冷启动耗时；已存在的集合会抛错，忽略即可
    collectionsReady = Promise.all(
      COLLECTIONS.map((name) => db.createCollection(name).catch(() => {}))
    );
  }
  return collectionsReady;
}

// 判断是否为「集合不存在」错误（兼容不同 SDK 版本的错误码/文案）
function isCollectionNotExist(e) {
  if (!e) return false;
  return e.errCode === -502005 || /collection.*not.*exist/i.test(e.errMsg || "");
}

// 读取单个文档：不存在或读取失败时返回 null，避免直接抛错
async function getDocOrNull(collection, id) {
  if (!id) return null;
  try {
    const res = await db.collection(collection).doc(String(id)).get();
    return res.data || null;
  } catch (e) {
    return null;
  }
}

// 交流栏目附件：服务端规范化，限制数量并剔除无效 fileID
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

// 删除文档后清理云存储附件，失败不影响主流程
async function removeCloudFiles(records) {
  const fileIDs = [];
  (records || []).forEach((r) => {
    (r.attachments || []).forEach((a) => {
      if (a && a.fileID) fileIDs.push(a.fileID);
    });
  });
  if (!fileIDs.length) return;
  try {
    await cloud.deleteFile({ fileList: fileIDs });
  } catch (e) {}
}

exports.main = async (event) => {
  const { OPENID } = cloud.getWXContext();
  const { action, data = {} } = event;

  try {
    await ensureCollections();
    const usersCol = db.collection("users");

    // 首次登录仅建立未注册档案，需完成登记后方可使用
    let user = null;
    try {
      const uRes = await usersCol.where({ _openid: OPENID }).limit(1).get();
      user = uRes.data[0];
    } catch (e) {
      if (!isCollectionNotExist(e)) throw e;
      await db.createCollection("users").catch(() => {});
    }

    if (!user) {
      await usersCol.add({
        data: {
          _openid: OPENID,
          nickname: "同窗",
          role: "student",
          registered: false,
          createTime: db.serverDate(),
        },
      });
      // 重新读取以拿到真实 _id；并发首次登录时取最早创建的档案
      const uRes = await usersCol
        .where({ _openid: OPENID })
        .orderBy("createTime", "asc")
        .limit(1)
        .get();
      user = uRes.data[0];
      if (!user) return { ok: false, msg: "账号档案创建失败，请重试" };
    }

    // 部署自检：小程序端可用 { action: "ping" } 验证云函数是否正常
    if (action === "ping") {
      return { ok: true, pong: true, openid: OPENID, time: Date.now() };
    }

    // 除登录、登记外的所有操作均要求已注册
    if (action !== "login" && action !== "register" && !user.registered) {
      return { ok: false, msg: "请先完成入斋登记" };
    }

    switch (action) {
      case "login":
        return { ok: true, user, registered: !!user.registered };

      case "register": {
        const name = String(data.name || "").trim();
        const studentId = String(data.studentId || "").trim();
        const className = String(data.className || "").trim();
        if (!name || !studentId || !className) {
          return { ok: false, msg: "请完整填写姓名、学号与班级" };
        }
        // 学号唯一，防止冒用他人学籍
        const dup = await usersCol.where({ studentId }).get();
        if (dup.data.some((u) => u._openid !== OPENID)) {
          return { ok: false, msg: "该学号已被注册" };
        }
        // 职务以数据库 roster 集合为准：学号命中则取库内职务，否则为普通同学
        const rosterRes = await db.collection("roster").where({ studentId }).get();
        const position = rosterRes.data[0] ? rosterRes.data[0].position : "普通同学";
        // 管理员职务 → admin；其余班委职务 → committee
        const role =
          position === "管理员"
            ? "admin"
            : position === "普通同学"
              ? "student"
              : "committee";
        await usersCol.doc(user._id).update({
          data: {
            name,
            studentId,
            className,
            position,
            role,
            nickname: name,
            registered: true,
            updateTime: db.serverDate(),
          },
        });
        return {
          ok: true,
          registered: true,
          user: { ...user, name, studentId, className, position, role, nickname: name, registered: true },
        };
      }

      case "setNickname": {
        await usersCol.doc(user._id).update({ data: { nickname: data.nickname } });
        return { ok: true };
      }

      // ===== 课程设置：仅管理员统一编辑（课代表由名单 roster 集合的 repSubjects 字段决定） =====
      case "saveSubjectSettings": {
        if (user.role !== "admin") return { ok: false, msg: "仅管理员可编辑课程设置" };
        const key = String(data.subject || "");
        if (!SUBJECT_KEYS.includes(key)) return { ok: false, msg: "板块不存在" };
        const teacher = String(data.teacher || "").trim();
        const credit = String(data.credit || "").trim();
        const schedule = String(data.schedule || "").trim();

        const payload = {
          subject: key,
          teacher,
          credit,
          schedule,
          updaterName: user.nickname,
          updateTime: db.serverDate(),
        };
        const settingsCol = db.collection("subject_settings");
        const exist = await settingsCol.where({ subject: key }).get();
        if (exist.data.length) {
          await settingsCol.doc(exist.data[0]._id).update({ data: payload });
        } else {
          await settingsCol.add({ data: payload });
        }
        return { ok: true, settings: payload };
      }

      // ===== 学科板块：名单指定的课代表（或班委/管理员）发布任务通知 =====
      case "publishSubjectTask": {
        if (!SUBJECT_KEYS.includes(data.subject)) return { ok: false, msg: "板块不存在" };
        const isRep = await isRepOf(user.studentId, data.subject);
        if (!isRep && !isManager(user)) {
          return { ok: false, msg: "仅该科目课代表、班委或管理员可发布任务通知" };
        }
        const title = String(data.title || "").trim();
        if (!title) return { ok: false, msg: "请填写任务通知标题" };
        const attachments = (Array.isArray(data.attachments) ? data.attachments : [])
          .slice(0, 5)
          .filter((a) => a && a.fileID)
          .map((a) => ({
            fileID: String(a.fileID),
            name: String(a.name || "附件"),
            type: a.type === "image" ? "image" : "file",
            size: Number(a.size) || 0,
          }));
        const res = await db.collection("subject_tasks").add({
          data: {
            subject: data.subject,
            title,
            content: String(data.content || "").trim(),
            deadline: data.deadline || 0, // 时间戳(ms)，0 表示不设截止
            attachments, // [{ fileID, name, type, size }]
            creatorOpenid: OPENID,
            creatorName: user.nickname,
            createTime: db.serverDate(),
          },
        });
        return { ok: true, id: res._id };
      }

      case "deleteSubjectTask": {
        const task = await getDocOrNull("subject_tasks", data.id);
        if (!task) return { ok: false, msg: "任务通知不存在" };
        const isRep = task.creatorOpenid === OPENID || (await isRepOf(user.studentId, task.subject));
        if (!isRep && !isManager(user)) {
          return { ok: false, msg: "无权删除该任务通知" };
        }
        const fileIDs = (task.attachments || []).map((a) => a.fileID).filter(Boolean);
        await db.collection("subject_tasks").doc(data.id).remove();
        // 一并清理云存储中的附件
        if (fileIDs.length) {
          try {
            await cloud.deleteFile({ fileList: fileIDs });
          } catch (e) {}
        }
        return { ok: true };
      }

      case "publishHomework": {
        if (!isManager(user)) return { ok: false, msg: "仅班委或管理员可发布作业" };
        if (!data.title || !data.deadline) return { ok: false, msg: "缺少标题或截止时间" };
        // 服务端规范化附件，避免客户端写入任意字段或超限
        const attachments = (Array.isArray(data.attachments) ? data.attachments : [])
          .slice(0, 5)
          .filter((a) => a && a.fileID)
          .map((a) => ({
            fileID: String(a.fileID),
            name: String(a.name || "附件"),
            type: a.type === "image" ? "image" : "file",
            size: Number(a.size) || 0,
          }));
        const res = await db.collection("homeworks").add({
          data: {
            title: data.title,
            desc: data.desc || "",
            deadline: data.deadline, // 时间戳(ms)
            attachments, // [{ fileID, name, type, size }]
            creatorOpenid: OPENID,
            creatorName: user.nickname,
            createTime: db.serverDate(),
          },
        });
        return { ok: true, id: res._id };
      }

      case "deleteHomework": {
        const hw = await getDocOrNull("homeworks", data.id);
        if (!hw) return { ok: false, msg: "作业不存在" };
        if (!isManager(user)) return { ok: false, msg: "仅班委或管理员可删除作业" };
        await db.collection("homeworks").doc(data.id).remove();
        // 删除云存储附件
        const fileIDs = (hw.attachments || []).map((a) => a.fileID).filter(Boolean);
        if (fileIDs.length) await cloud.deleteFile({ fileList: fileIDs });
        return { ok: true };
      }

      // ===== 志愿活动栏目：仅宣传委员（或管理员）可发布 =====
      case "createVolunteer": {
        if (user.position !== "宣传委员" && user.role !== "admin") {
          return { ok: false, msg: "仅宣传委员可发布志愿活动" };
        }
        const content = String(data.content || "").trim();
        const place = String(data.place || "").trim();
        const signupMethod = String(data.signupMethod || "小程序内报名").trim() || "小程序内报名";
        const activityStartTime = Number(data.activityStartTime) || 0;
        const activityEndTime = Number(data.activityEndTime) || 0;
        if (!data.title || !data.endTime) return { ok: false, msg: "缺少志愿活动主题或报名截止时间" };
        if (!content || !place || !activityStartTime || !activityEndTime || activityEndTime <= activityStartTime) {
          return { ok: false, msg: "请选择有效的志愿活动时间段" };
        }
        const res = await db.collection("sign_tasks").add({
          data: {
            title: data.title,
            content,
            place,
            activityStartTime, // 活动开始时间戳(ms)
            activityEndTime, // 活动结束时间戳(ms)
            activityTime: activityStartTime, // 兼容旧的按开始时间读取/排序
            signupMethod, // 报名方式
            startTime: data.startTime || Date.now(),
            endTime: data.endTime, // 报名截止时间戳(ms)
            creatorOpenid: OPENID,
            creatorName: user.nickname,
            createTime: db.serverDate(),
          },
        });
        return { ok: true, id: res._id };
      }

      case "joinVolunteer": {
        if (!data.taskId) return { ok: false, msg: "缺少志愿活动" };
        const taskRes = await db.collection("sign_tasks").doc(data.taskId).get();
        const task = taskRes.data;
        if (!task) return { ok: false, msg: "志愿活动不存在" };
        const now = Date.now();
        if (now < task.startTime) return { ok: false, msg: "报名尚未开始" };
        if (now > task.endTime) return { ok: false, msg: "报名已截止" };
        const dup = await db
          .collection("sign_records")
          .where({ taskId: data.taskId, _openid: OPENID })
          .get();
        if (dup.data.length) return { ok: false, msg: "您已报名，无需重复操作" };
        await db.collection("sign_records").add({
          data: {
            taskId: data.taskId,
            taskTitle: task.title,
            _openid: OPENID,
            nickname: user.nickname,
            signTime: db.serverDate(), // 云端记录报名时间
            onTime: true,
            createTime: db.serverDate(),
          },
        });
        return { ok: true, msg: "报名成功" };
      }

      // ===== 活动栏目：班委或管理员发布 =====
      case "publishActivity": {
        if (!isManager(user)) return { ok: false, msg: "仅班委或管理员可发布活动" };
        if (!data.title || !data.activityTime) return { ok: false, msg: "缺少活动标题或时间" };
        const res = await db.collection("activities").add({
          data: {
            title: data.title,
            place: data.place || "待定",
            content: data.content || "",
            activityTime: data.activityTime, // 时间戳(ms)
            creatorOpenid: OPENID,
            creatorName: user.nickname,
            createTime: db.serverDate(),
          },
        });
        return { ok: true, id: res._id };
      }

      case "deleteActivity": {
        if (!isManager(user)) return { ok: false, msg: "仅班委或管理员可删除活动" };
        await db.collection("activities").doc(data.id).remove();
        return { ok: true };
      }

      // ===== 交流栏目：所有人可自由发帖、回复（可带文件 / 照片） =====
      case "createPost": {
        const content = (data.content || "").trim();
        const attachments = normalizeAttachments(data.attachments);
        if (!content && !attachments.length) return { ok: false, msg: "请填写内容或添加附件" };
        if (content.length > 1000) return { ok: false, msg: "内容过长" };
        const res = await db.collection("posts").add({
          data: {
            content,
            attachments, // [{ fileID, name, type, size }]
            _openid: OPENID,
            nickname: user.nickname,
            role: user.role,
            replyCount: 0,
            createTime: db.serverDate(),
          },
        });
        return { ok: true, id: res._id };
      }

      case "createReply": {
        const content = (data.content || "").trim();
        const attachments = normalizeAttachments(data.attachments);
        if (!content && !attachments.length) return { ok: false, msg: "请填写回复或添加附件" };
        if (content.length > 500) return { ok: false, msg: "回复过长" };
        const post = await getDocOrNull("posts", data.postId);
        if (!post) return { ok: false, msg: "帖子不存在" };
        await db.collection("post_replies").add({
          data: {
            postId: data.postId,
            content,
            attachments, // [{ fileID, name, type, size }]
            _openid: OPENID,
            nickname: user.nickname,
            role: user.role,
            createTime: db.serverDate(),
          },
        });
        await db.collection("posts").doc(data.postId).update({
          data: { replyCount: _.inc(1) },
        });
        return { ok: true };
      }

      case "deletePost": {
        const post = await getDocOrNull("posts", data.id);
        if (!post) return { ok: false, msg: "帖子不存在" };
        if (post._openid !== OPENID && !isManager(user)) {
          return { ok: false, msg: "只能删除自己的帖子" };
        }
        const replies = await db
          .collection("post_replies")
          .where({ postId: data.id })
          .limit(1000)
          .get();
        await db.collection("posts").doc(data.id).remove();
        await db.collection("post_replies").where({ postId: data.id }).remove();
        // 帖子与全部回复的附件一并从云存储清理
        await removeCloudFiles([post, ...(replies.data || [])]);
        return { ok: true };
      }

      default:
        return { ok: false, msg: "未知操作" };
    }
  } catch (e) {
    console.error("api error", action, e);
    if (isCollectionNotExist(e)) {
      return { ok: false, msg: "数据库集合不存在，请稍后重试（首次使用会自动创建）" };
    }
    const raw = (e && (e.errMsg || e.message)) || "";
    if (/permission|denied/i.test(raw)) {
      return { ok: false, msg: "数据库权限不足，请检查集合权限设置" };
    }
    return { ok: false, msg: raw || "服务异常，请稍后重试" };
  }
};
