// 云函数 api：统一处理写操作，服务端校验权限
const cloud = require("wx-server-sdk");
cloud.init({ env: cloud.DYNAMIC_CURRENT_ENV });
const db = cloud.database();
const _ = db.command;

// 学号 → 职务名单字典（管理员/班委/普通同学均以名单为准）
const { ROSTER, ADMIN_IDS } = require("./roster");

// 班委或管理员均视为管理角色（可发布/删除作业、活动等）
const isManager = (u) => u.role === "committee" || u.role === "admin";

// 学科板块白名单（与小程序端 utils/subjects.js 保持一致）
const SUBJECT_KEYS = [
  "english", "math", "psychology", "ai", "physics",
  "defense", "ideology", "pe", "career",
];

exports.main = async (event) => {
  const { OPENID } = cloud.getWXContext();
  const { action, data = {} } = event;
  const usersCol = db.collection("users");

  try {
    // 首次登录仅建立未注册档案，需完成登记后方可使用
    const uRes = await usersCol.where({ _openid: OPENID }).get();
    let user = uRes.data[0];
    if (!user) {
      const addRes = await usersCol.add({
        data: {
          _openid: OPENID,
          nickname: "同窗",
          role: "student",
          registered: false,
          createTime: db.serverDate(),
        },
      });
      user = {
        _id: addRes._id,
        _openid: OPENID,
        nickname: "同窗",
        role: "student",
        registered: false,
      };
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
        // 课代表由管理员在课程设置中统一指定，登记时不可自行选择
        const subject = user.subject || "";
        // 学号唯一，防止冒用他人学籍
        const dup = await usersCol.where({ studentId }).get();
        if (dup.data.some((u) => u._openid !== OPENID)) {
          return { ok: false, msg: "该学号已被注册" };
        }
        // 职务以名单字典为准：学号命中则取字典职务，否则为普通同学
        const position = ROSTER[studentId] || "普通同学";
        // 管理员学号或管理员职务 → admin；其余班委职务 → committee
        const role =
          position === "管理员" || ADMIN_IDS.includes(studentId)
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
            subject,
            role,
            nickname: name,
            registered: true,
            updateTime: db.serverDate(),
          },
        });
        return {
          ok: true,
          registered: true,
          user: { ...user, name, studentId, className, position, subject, role, nickname: name, registered: true },
        };
      }

      case "setNickname": {
        await usersCol.doc(user._id).update({ data: { nickname: data.nickname } });
        return { ok: true };
      }

      // ===== 课程设置：仅管理员统一编辑（原班长权限，班长职务已移除） =====
      case "saveSubjectSettings": {
        if (user.role !== "admin") return { ok: false, msg: "仅管理员可编辑课程设置" };
        const key = String(data.subject || "");
        if (!SUBJECT_KEYS.includes(key)) return { ok: false, msg: "板块不存在" };
        const teacher = String(data.teacher || "").trim();
        const credit = String(data.credit || "").trim();
        const schedule = String(data.schedule || "").trim();
        const repOpenid = String(data.repOpenid || "").trim();
        let repName = "";
        let rep = null;

        if (repOpenid) {
          const repRes = await usersCol.where({ _openid: repOpenid, registered: true }).get();
          rep = repRes.data[0];
          if (!rep) return { ok: false, msg: "所选课代表不存在" };
          repName = rep.name || rep.nickname;
        }
        // 先清除该科目原课代表，再指定新任
        await usersCol.where({ subject: key }).update({ data: { subject: "" } });
        if (rep) await usersCol.doc(rep._id).update({ data: { subject: key } });

        const payload = {
          subject: key,
          teacher,
          credit,
          schedule,
          repOpenid,
          repName,
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

      // ===== 学科板块：课程设置指定的课代表（或班委）发布任务通知 =====
      case "publishSubjectTask": {
        if (!SUBJECT_KEYS.includes(data.subject)) return { ok: false, msg: "板块不存在" };
        const settingsRes = await db.collection("subject_settings").where({ subject: data.subject }).get();
        const settings = settingsRes.data[0];
        // 兼容管理员尚未设置课代表的情况
        const isRep = settings ? settings.repOpenid === OPENID : user.subject === data.subject;
        if (!isRep && !isManager(user)) {
          return { ok: false, msg: "仅该科目课代表、班委或管理员可发布任务通知" };
        }
        const title = String(data.title || "").trim();
        if (!title) return { ok: false, msg: "请填写任务通知标题" };
        const res = await db.collection("subject_tasks").add({
          data: {
            subject: data.subject,
            title,
            content: String(data.content || "").trim(),
            deadline: data.deadline || 0, // 时间戳(ms)，0 表示不设截止
            creatorOpenid: OPENID,
            creatorName: user.nickname,
            createTime: db.serverDate(),
          },
        });
        return { ok: true, id: res._id };
      }

      case "deleteSubjectTask": {
        const taskRes = await db.collection("subject_tasks").doc(data.id).get();
        const task = taskRes.data;
        if (!task) return { ok: false, msg: "任务通知不存在" };
        if (task.creatorOpenid !== OPENID && user.subject !== task.subject && !isManager(user)) {
          return { ok: false, msg: "无权删除该任务通知" };
        }
        await db.collection("subject_tasks").doc(data.id).remove();
        return { ok: true };
      }

      case "publishHomework": {
        if (!isManager(user)) return { ok: false, msg: "仅班委或管理员可发布作业" };
        if (!data.title || !data.deadline) return { ok: false, msg: "缺少标题或截止时间" };
        const res = await db.collection("homeworks").add({
          data: {
            title: data.title,
            desc: data.desc || "",
            deadline: data.deadline, // 时间戳(ms)
            attachments: data.attachments || [], // [{ fileID, name }]
            creatorOpenid: OPENID,
            creatorName: user.nickname,
            createTime: db.serverDate(),
          },
        });
        return { ok: true, id: res._id };
      }

      case "deleteHomework": {
        const hwRes = await db.collection("homeworks").doc(data.id).get();
        const hw = hwRes.data;
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
        if (!data.title || !data.endTime) return { ok: false, msg: "缺少志愿活动主题或报名截止时间" };
        const res = await db.collection("sign_tasks").add({
          data: {
            title: data.title,
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

      // ===== 交流栏目：所有人可自由发帖、回复 =====
      case "createPost": {
        const content = (data.content || "").trim();
        if (!content) return { ok: false, msg: "内容不能为空" };
        if (content.length > 1000) return { ok: false, msg: "内容过长" };
        const res = await db.collection("posts").add({
          data: {
            content,
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
        if (!content) return { ok: false, msg: "回复不能为空" };
        if (content.length > 500) return { ok: false, msg: "回复过长" };
        const postRes = await db.collection("posts").doc(data.postId).get();
        if (!postRes.data) return { ok: false, msg: "帖子不存在" };
        await db.collection("post_replies").add({
          data: {
            postId: data.postId,
            content,
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
        const postRes = await db.collection("posts").doc(data.id).get();
        const post = postRes.data;
        if (!post) return { ok: false, msg: "帖子不存在" };
        if (post._openid !== OPENID && !isManager(user)) {
          return { ok: false, msg: "只能删除自己的帖子" };
        }
        await db.collection("posts").doc(data.id).remove();
        await db.collection("post_replies").where({ postId: data.id }).remove();
        return { ok: true };
      }

      default:
        return { ok: false, msg: "未知操作" };
    }
  } catch (e) {
    console.error(e);
    return { ok: false, msg: e.errMsg || "服务异常" };
  }
};
