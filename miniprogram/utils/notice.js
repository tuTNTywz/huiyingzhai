// 通知中心：站内通知读取、未读计算、微信订阅消息授权
const { formatTime } = require("./util");
const { subjectName } = require("./subjects");

// 在微信公众平台「订阅消息」申请「新作业提醒」类模板后填入模板 ID
// 留空时只使用站内通知，不影响作业发布与查看
const HOMEWORK_TMPL_ID = "6avlHCT0IR-fd1hk3c15hQDn0X0VWmuQs0rcdbN5FgM";

const LAST_READ_KEY = "notice_last_read";
// 提醒开关状态（本机记录）：开启时云端 subscriptions.remain 记录剩余推送次数
const PUSH_ENABLED_KEY = "notice_push_enabled";

function isPushEnabled() {
  return !!wx.getStorageSync(PUSH_ENABLED_KEY);
}

function setPushEnabled(enabled) {
  enabled ? wx.setStorageSync(PUSH_ENABLED_KEY, true) : wx.removeStorageSync(PUSH_ENABLED_KEY);
}

function getLastRead() {
  return Number(wx.getStorageSync(LAST_READ_KEY)) || 0;
}

// 广播式通知：所有同学读同一份记录，未读状态按本机「上次阅读时间」计算
// 通过云函数读取（notifications 集合默认权限仅创建者可读写，小程序端直读会被拒）
function fetchNotices() {
  return wx.cloud
    .callFunction({ name: "api", data: { action: "fetchHomeworkNotices" } })
    .then((r) => {
      const res = r.result || {};
      if (!res.ok) throw new Error(res.msg || "通知加载失败");
      const last = getLastRead();
      const myOpenid = (getApp().globalData.userInfo || {})._openid || "";
      // 发布者本人不展示自己发布的通知
      const list = (res.list || [])
        .filter((n) => !n.publisherOpenid || n.publisherOpenid !== myOpenid)
        .map((n) => {
        const ms = n.createMs || 0;
        const deadlineMs = n.deadlineMs || 0;
        return {
          ...n,
          timeText: ms ? formatTime(ms) : "",
          deadlineText: deadlineMs ? formatTime(deadlineMs) : "",
          unread: ms > last,
          // 学科任务显示科目名，班级作业显示「新作业」
          tagText: n.type === "subject_task" ? subjectName(n.subject) || "科目任务" : "新作业",
        };
      });
      return { list, unread: list.filter((n) => n.unread).length };
    });
}

// 进入通知中心后把当前最新一条标记为已读（下次进入不再显示未读）
function markAllRead(list) {
  const newest = (list || []).reduce(
    (max, n) => Math.max(max, n.createMs || 0),
    getLastRead()
  );
  if (newest) wx.setStorageSync(LAST_READ_KEY, newest);
}

function getUnreadCount() {
  return fetchNotices()
    .then((r) => r.unread)
    .catch(() => 0);
}

// 订阅消息授权：必须由用户点击触发，成功后由云函数登记一次可推送次数
function requestHomeworkPush() {
  return new Promise((resolve, reject) => {
    if (!HOMEWORK_TMPL_ID) return reject("尚未配置订阅消息模板 ID");
    wx.requestSubscribeMessage({
      tmplIds: [HOMEWORK_TMPL_ID],
      success: (res) => {
        if (res[HOMEWORK_TMPL_ID] !== "accept") return reject("未开启提醒");
        wx.cloud
          .callFunction({ name: "api", data: { action: "subscribeHomework" } })
          .then((r) => {
            if (r.result && r.result.ok) {
              setPushEnabled(true);
              resolve();
            } else {
              reject((r.result && r.result.msg) || "登记提醒失败");
            }
          })
          .catch(() => reject("登记提醒失败"));
      },
      fail: () => reject("授权未完成"),
    });
  });
}

// 关闭提醒：云端清零剩余推送次数（微信侧授权无法撤销）
function disableHomeworkPush() {
  setPushEnabled(false);
  return wx.cloud
    .callFunction({ name: "api", data: { action: "unsubscribeHomework" } })
    .then((r) => {
      if (!(r.result && r.result.ok)) throw new Error("关闭失败");
    });
}

// 进入通知中心时顺手续订一次额度（开关保持开启即自动续）
// 关键：必须在用户点击回调内「同步」发起 requestSubscribeMessage，
// 若先异步查询再请求会丢失点击上下文导致静默失败。
// 用户勾选过「总是保持以上选择」则全程无感；未勾选则每次进入会弹一次授权。
function renewPushOnEntry() {
  if (!HOMEWORK_TMPL_ID) return Promise.reject("未配置模板");
  return new Promise((resolve) => {
    wx.requestSubscribeMessage({
      tmplIds: [HOMEWORK_TMPL_ID],
      success: (res) => {
        if (res[HOMEWORK_TMPL_ID] !== "accept") return resolve("declined");
        wx.cloud
          .callFunction({ name: "api", data: { action: "subscribeHomework" } })
          .then((r) => {
            if (r.result && r.result.ok) {
              setPushEnabled(true);
              resolve("renewed");
            } else {
              resolve("error");
            }
          })
          .catch(() => resolve("error"));
      },
      fail: () => resolve("declined"),
    });
  });
}

module.exports = {
  HOMEWORK_TMPL_ID,
  fetchNotices,
  markAllRead,
  getUnreadCount,
  isPushEnabled,
  requestHomeworkPush,
  disableHomeworkPush,
  renewPushOnEntry,
};
