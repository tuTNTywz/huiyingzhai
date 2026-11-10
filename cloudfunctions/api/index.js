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

// 微信订阅消息模板 ID：在公众平台「订阅消息」申请「新作业提醒」类模板后填入
// 留空时只写站内通知，不影响作业发布
const HOMEWORK_TMPL_ID = "6avlHCT0IR-fd1hk3c15hQDn0X0VWmuQs0rcdbN5FgM";

// 推送目标的小程序版本：开发版 "developer" / 体验版 "trial" / 正式版 "formal"
// 与当前实际使用的版本不一致时，服务通知将无法送达或无法跳转；发布正式版后改为 "formal"
const PUSH_MINIPROGRAM_STATE = "trial";

// 业务用到的全部集合：冷启动时自动创建，避免「集合不存在」导致登录/写入失败
const COLLECTIONS = [
  "users",
  "roster",
  "homeworks",
  "sign_tasks",
  "sign_records",
  "activities",
  "posts",
  "post_replies",
  "subject_settings",
  "subject_tasks",
  "notifications",
  "subscriptions",
];
let collectionsReady = null;

function ensureCollections() {
  if (!collectionsReady) {
    // 并行创建以缩短冷启动耗时；已存在的集合会抛错，忽略即可
    // 整体再兜底 catch：自动建集合失败绝不能连带登录/其它操作一起失败
    try {
      collectionsReady = Promise.all(
        COLLECTIONS.map((name) => db.createCollection(name).catch(() => {}))
      ).catch(() => {});
    } catch (e) {
      console.error("自动创建集合失败", e);
      collectionsReady = Promise.resolve();
    }
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

// 订阅消息要求的日期格式：yyyy-MM-dd HH:mm
function noticeTimeText(ts) {
  const d = new Date(Number(ts) || Date.now());
  const p = (n) => (n < 10 ? "0" + n : "" + n);
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 时间值统一转毫秒时间戳（兼容 Date / ISO 字符串 / 数字）
function toMs(v) {
  if (!v) return 0;
  if (typeof v === "number") return v;
  const d = new Date(v);
  return isNaN(d.getTime()) ? 0 : d.getTime();
}

// 给已订阅的同学推送微信服务通知；单个用户失败不影响其它人
// excludeOpenid 为发布者本人，不向其推送
// 模板字段：thing1=作业/任务名，time4=结束时间，thing5=备注（作业与学科任务共用同一模板）
async function pushSubscribeMessage({ page, title, time, remark, excludeOpenid }) {
  if (!HOMEWORK_TMPL_ID) return;
  try {
    const subs = await db
      .collection("subscriptions")
      .where({ remain: _.gt(0) })
      .limit(1000)
      .get();
    const payload = {
      templateId: HOMEWORK_TMPL_ID,
      page,
      miniprogramState: PUSH_MINIPROGRAM_STATE,
      lang: "zh_CN",
      data: {
        thing1: { value: String(title || "新通知").slice(0, 20) },
        time4: { value: noticeTimeText(time) },
        thing5: {
          value: String(remark || "").trim().slice(0, 20) || "点击查看详情",
        },
      },
    };
    // 排除发布者本人
    const targets = subs.data.filter((s) => s._openid !== excludeOpenid);
    await Promise.all(
      targets.map(async (s) => {
        try {
          await cloud.openapi.subscribeMessage.send({ ...payload, touser: s._openid });
          await db.collection("subscriptions").doc(s._id).update({
            data: { remain: _.inc(-1), updateTime: db.serverDate() },
          });
        } catch (e) {
          // 43101 = 订阅次数已用完或用户拒收，把剩余次数归零并跳过
          console.error("推送失败", s._openid, e.errCode || e.errMsg || e.message);
          if (e.errCode === 43101 || /43101/.test(JSON.stringify(e))) {
            await db
              .collection("subscriptions")
              .doc(s._id)
              .update({ data: { remain: 0, updateTime: db.serverDate() } })
              .catch(() => {});
          }
        }
      })
    );
  } catch (e) {
    console.error("推送流程异常", e.errMsg || e.message || e);
  }
}

// 班级作业推送：作业名称栏显示「班级作业：标题」，点击进入作业详情
const pushHomeworkNotice = ({ homeworkId, title, deadline, content, publisherOpenid }) =>
  pushSubscribeMessage({
    page: `pages/homework/detail?id=${homeworkId}`,
    title: `班级作业：${title || ""}`,
    time: deadline,
    remark: content,
    excludeOpenid: publisherOpenid,
  });

// 学科任务推送：作业名称栏显示「科目名：标题」，点击进入对应学科板块
const pushSubjectTaskNotice = ({ subject, taskId, title, deadline, content, publisherOpenid }) =>
  pushSubscribeMessage({
    page: `pages/subject/index?key=${subject}`,
    title: `${SUBJECT_NAMES[subject] || "科目任务"}：${title || ""}`,
    time: deadline || Date.now(),
    remark: content,
    excludeOpenid: publisherOpenid,
  });

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
        let position = "普通同学";
        try {
          const rosterRes = await db.collection("roster").where({ studentId }).get();
          if (rosterRes.data[0]) position = rosterRes.data[0].position || "普通同学";
        } catch (e) {
          console.error("读取名单失败", e);
          return { ok: false, msg: "名单读取失败，请检查数据库 roster 集合是否已创建" };
        }
        // 管理员/班长职务 → admin；其余班委职务 → committee
        const role =
          position === "管理员" || position === "班长"
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
        // 生成站内通知，全体同学在通知中心可见（失败不影响发布本身）
        try {
          await db.collection("notifications").add({
            data: {
              type: "subject_task",
              taskId: res._id,
              subject: data.subject,
              title,
              content: String(data.content || "").trim().slice(0, 100) || "新任务已发布，点击查看详情",
              deadline: data.deadline || 0,
              creatorName: user.nickname,
              publisherOpenid: OPENID, // 发布者本人不在通知中心展示此条
              createTime: db.serverDate(),
            },
          });
        } catch (e) {
          console.error("学科任务站内通知写入失败", e.errMsg || e.message || e);
        }
        // 微信服务通知：已订阅的同学收到学科任务提醒（发布者除外）
        await pushSubjectTaskNotice({
          subject: data.subject,
          taskId: res._id,
          title,
          deadline: data.deadline || 0,
          content: String(data.content || "").trim(),
          publisherOpenid: OPENID,
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
        // 同步移除该任务产生的站内通知
        try {
          await db.collection("notifications").where({ taskId: data.id }).remove();
        } catch (e) {}
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

        // 通知失败不影响作业发布成功本身
        try {
          // 发布成功后生成一条站内通知，全体同学进入通知中心即可看到
          await db.collection("notifications").add({
            data: {
              type: "homework",
              homeworkId: res._id,
              title: String(data.title),
              content: String(data.desc || "").trim().slice(0, 100) || "新作业已发布，点击查看详情",
              deadline: data.deadline,
              creatorName: user.nickname,
              publisherOpenid: OPENID, // 发布者本人不在通知中心展示此条
              createTime: db.serverDate(),
            },
          });
          // 微信服务通知：配置了模板 ID 且同学已订阅时推送（发布者除外）
          await pushHomeworkNotice({
            homeworkId: res._id,
            title: data.title,
            deadline: data.deadline,
            content: data.desc,
            publisherOpenid: OPENID,
          });
        } catch (e) {}
        return { ok: true, id: res._id };
      }

      // 同学点击「开启作业提醒」授权后，登记一次可推送次数
      case "subscribeHomework": {
        if (!HOMEWORK_TMPL_ID) return { ok: false, msg: "尚未配置订阅消息模板 ID" };
        const col = db.collection("subscriptions");
        const exist = await col.where({ _openid: OPENID }).limit(1).get();
        if (exist.data.length) {
          await col.doc(exist.data[0]._id).update({
            data: { remain: _.inc(1), updateTime: db.serverDate() },
          });
        } else {
          await col.add({
            data: { _openid: OPENID, remain: 1, createTime: db.serverDate() },
          });
        }
        return { ok: true };
      }

      // 关闭作业提醒：剩余推送次数清零（微信侧授权无法撤销，仅云端不再推送）
      case "unsubscribeHomework": {
        const col = db.collection("subscriptions");
        const exist = await col.where({ _openid: OPENID }).limit(1).get();
        if (exist.data.length) {
          await col.doc(exist.data[0]._id).update({
            data: { remain: 0, updateTime: db.serverDate() },
          });
        }
        return { ok: true };
      }

      // 通知中心：站内通知统一走云函数读取
      // （notifications 集合由云函数自动创建，默认「仅创建者可读写」，
      //   小程序端直接读会因权限被拒，故改为服务端返回）
      case "fetchHomeworkNotices": {
        const res = await db
          .collection("notifications")
          .orderBy("createTime", "desc")
          .limit(50)
          .get();
        const list = (res.data || []).map((n) => ({
          ...n,
          createMs: toMs(n.createTime),
          deadlineMs: toMs(n.deadline),
        }));
        // 返回当前用户剩余推送额度，供小程序端在额度不足时自动续订
        const subRes = await db
          .collection("subscriptions")
          .where({ _openid: OPENID })
          .limit(1)
          .get();
        const pushRemain = subRes.data[0] ? subRes.data[0].remain || 0 : 0;
        return { ok: true, list, pushRemain };
      }

      case "deleteHomework": {
        const hw = await getDocOrNull("homeworks", data.id);
        if (!hw) return { ok: false, msg: "作业不存在" };
        if (!isManager(user)) return { ok: false, msg: "仅班委或管理员可删除作业" };
        await db.collection("homeworks").doc(data.id).remove();
        // 同步移除该作业产生的通知
        try {
          await db.collection("notifications").where({ homeworkId: data.id }).remove();
        } catch (e) {}
        // 删除云存储附件
        const fileIDs = (hw.attachments || []).map((a) => a.fileID).filter(Boolean);
        if (fileIDs.length) {
          try {
            await cloud.deleteFile({ fileList: fileIDs });
          } catch (e) {}
        }
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
