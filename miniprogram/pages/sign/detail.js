// 志愿活动详情：同学在线报名，查看报名记录
const app = getApp();
const api = require("../../utils/api");
const { formatTime, formatTimeRange, toDateMs } = require("../../utils/util");

Page({
  data: {
    task: null,
    records: [],
    myRecord: null,
    status: "", // active / ended
    isCommittee: false,
    signing: false,
    countText: "",
  },

  onLoad(options) {
    this.id = options.id;
  },

  onShow() {
    const refresh = () => this.setData({ isCommittee: app.isCommittee() });
    if (app.globalData.userInfo) refresh();
    else app.userReadyCallback = refresh;
    this.fetchAll();
  },

  onPullDownRefresh() {
    this.fetchAll();
    wx.stopPullDownRefresh();
  },

  fetchAll() {
    api
      .call("getSignTask", { id: this.id })
      .then((res) => {
        if (!res.ok) throw res.msg;
        const t = res.item;
        const end = toDateMs(t.endTime) || t.endTime;
        const status = Date.now() <= end ? "active" : "ended";
        const legacyMs = toDateMs(t.activityTime) || t.activityTime || 0;
        const startMs = toDateMs(t.activityStartTime) || t.activityStartTime || legacyMs;
        const endMs = toDateMs(t.activityEndTime) || t.activityEndTime || 0;
        const activityText = endMs
          ? formatTimeRange(startMs, endMs)
          : startMs ? formatTime(startMs) : "";
        this.setData({
          task: {
            ...t,
            endText: formatTime(end),
            activityText,
            signupMethod: t.signupMethod || "小程序内报名",
          },
          status,
        });
        // 查询报名记录
        return api.call("listSignRecords", { taskId: this.id }).then((r) => {
          if (!r.ok) throw r.msg;
          const me = app.globalData.userInfo;
          const records = (r.list || []).map((x) => ({
            ...x,
            timeText: x.signTime ? formatTime(toDateMs(x.signTime)) : "",
          }));
          const myRecord = records.find((x) => x._openid === (me && me._openid)) || null;
          this.setData({
            records,
            myRecord,
            countText: `共 ${records.length} 人报名`,
          });
        });
      })
      .catch((msg) =>
        wx.showToast({
          title: typeof msg === "string" ? msg : "志愿活动不存在",
          icon: "none",
        })
      );
  },

  // 同学在线报名
  doSign() {
    if (this.data.signing) return;
    this.setData({ signing: true });
    api
      .call("joinVolunteer", { taskId: this.id })
      .then((res) => {
        if (res.ok) {
          wx.showToast({ title: "报名成功", icon: "success" });
          this.fetchAll();
        } else {
          wx.showModal({ title: "报名失败", content: res.msg, showCancel: false });
        }
      })
      .catch(() => wx.showToast({ title: "网络异常", icon: "none" }))
      .finally(() => this.setData({ signing: false }));
  },
});
