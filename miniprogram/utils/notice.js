// 通知中心：站内通知读取、未读计算、微信订阅消息授权
const { formatTime } = require("./util");

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
      const list = (res.list || []).map((n) => {
        const ms = n.createMs || 0;
        const deadlineMs = n.deadlineMs || 0;
        return {
          ...n,
          timeText: ms ? formatTime(ms) : "",
          deadlineText: deadlineMs ? formatTime(deadlineMs) : "",
          unread: ms > last,
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

module.exports = {
  HOMEWORK_TMPL_ID,
  fetchNotices,
  markAllRead,
  getUnreadCount,
  isPushEnabled,
  requestHomeworkPush,
  disableHomeworkPush,
};
