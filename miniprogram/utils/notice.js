// 通知中心：站内通知读取、未读计算、微信订阅消息授权
const { formatTime, toDateMs } = require("./util");

// 在微信公众平台「订阅消息」申请「新作业提醒」类模板后填入模板 ID
// 留空时只使用站内通知，不影响作业发布与查看
const HOMEWORK_TMPL_ID = "6avlHCT0IR-fd1hk3c15hXIrvAOeoJCK_Uwc9XVsiVg";

const LAST_READ_KEY = "notice_last_read";

function getLastRead() {
  return Number(wx.getStorageSync(LAST_READ_KEY)) || 0;
}

// 广播式通知：所有同学读同一份记录，未读状态按本机「上次阅读时间」计算
function fetchNotices() {
  return wx.cloud
    .database()
    .collection("notifications")
    .orderBy("createTime", "desc")
    .limit(50)
    .get()
    .then((res) => {
      const last = getLastRead();
      const list = (res.data || []).map((n) => {
        const ms = toDateMs(n.createTime);
        const deadlineMs = toDateMs(n.deadline) || n.deadline || 0;
        return {
          ...n,
          createMs: ms,
          timeText: formatTime(ms),
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
          .then((r) =>
            r.result && r.result.ok ? resolve() : reject((r.result && r.result.msg) || "登记提醒失败")
          )
          .catch(() => reject("登记提醒失败"));
      },
      fail: () => reject("授权未完成"),
    });
  });
}

module.exports = {
  HOMEWORK_TMPL_ID,
  fetchNotices,
  markAllRead,
  getUnreadCount,
  requestHomeworkPush,
};
