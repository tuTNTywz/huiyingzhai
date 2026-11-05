// 云函数 api：统一处理写操作，服务端校验班委权限
const cloud = require("wx-server-sdk");
cloud.init({ env: cloud.DYNAMIC_CURRENT_ENV });
const db = cloud.database();
const _ = db.command;

// 职位列表：除「普通同学」外均为班委职务，注册时据此确定角色
const POSITIONS = [
  "普通同学", "班长", "副班长", "团支书", "学习委员", "生活委员",
  "文艺委员", "体育委员", "组织委员", "宣传委员", "纪律委员",
];
const COMMITTEE_POSITIONS = POSITIONS.filter((p) => p !== "普通同学");

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
        const position = String(data.position || "").trim();
        if (!name || !studentId || !className || !position) {
          return { ok: false, msg: "请完整填写姓名、学号、班级与职位" };
        }
        if (!POSITIONS.includes(position)) return { ok: false, msg: "职位无效" };
        // 学号唯一，防止冒用他人学籍
        const dup = await usersCol.where({ studentId }).get();
        if (dup.data.some((u) => u._openid !== OPENID)) {
          return { ok: false, msg: "该学号已被注册" };
        }
        const role = COMMITTEE_POSITIONS.includes(position) ? "committee" : "student";
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

      case "publishHomework": {
        if (user.role !== "committee") return { ok: false, msg: "仅班委可发布作业" };
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
        if (user.role !== "committee") return { ok: false, msg: "仅班委可删除作业" };
        await db.collection("homeworks").doc(data.id).remove();
        // 删除云存储附件
        const fileIDs = (hw.attachments || []).map((a) => a.fileID).filter(Boolean);
        if (fileIDs.length) await cloud.deleteFile({ fileList: fileIDs });
        return { ok: true };
      }

      case "createSignTask": {
        if (user.role !== "committee") return { ok: false, msg: "仅班委可发起签到" };
        if (!data.title || !data.endTime) return { ok: false, msg: "缺少签到主题或截止时间" };
        const res = await db.collection("sign_tasks").add({
          data: {
            title: data.title,
            startTime: data.startTime || Date.now(),
            endTime: data.endTime, // 时间戳(ms)
            creatorOpenid: OPENID,
            creatorName: user.nickname,
            createTime: db.serverDate(),
          },
        });
        return { ok: true, id: res._id };
      }

      case "signIn": {
        if (!data.taskId) return { ok: false, msg: "缺少签到任务" };
        const taskRes = await db.collection("sign_tasks").doc(data.taskId).get();
        const task = taskRes.data;
        if (!task) return { ok: false, msg: "签到任务不存在" };
        const now = Date.now();
        if (now < task.startTime) return { ok: false, msg: "签到尚未开始" };
        if (now > task.endTime) return { ok: false, msg: "签到已截止" };
        const dup = await db
          .collection("sign_records")
          .where({ taskId: data.taskId, _openid: OPENID })
          .get();
        if (dup.data.length) return { ok: false, msg: "您已签到，无需重复操作" };
        await db.collection("sign_records").add({
          data: {
            taskId: data.taskId,
            taskTitle: task.title,
            _openid: OPENID,
            nickname: user.nickname,
            signTime: db.serverDate(), // 云端记录签到时间
            onTime: true,
            createTime: db.serverDate(),
          },
        });
        return { ok: true, msg: "签到成功" };
      }

      // ===== 活动栏目：仅班委发布 =====
      case "publishActivity": {
        if (user.role !== "committee") return { ok: false, msg: "仅班委可发布活动" };
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
        if (user.role !== "committee") return { ok: false, msg: "仅班委可删除活动" };
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
        if (post._openid !== OPENID && user.role !== "committee") {
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
