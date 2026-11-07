// 志愿活动详情：同学在线报名，查看报名记录
const app = getApp();
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
    const db = wx.cloud.database();
    db.collection("sign_tasks")
      .doc(this.id)
      .get()
      .then((res) => {
        const t = res.data;
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
        // 查询报名记录（所有用户可读）
        return db
          .collection("sign_records")
          .where({ taskId: this.id })
          .orderBy("createTime", "asc")
          .limit(100)
          .get()
          .then((r) => {
            const me = app.globalData.userInfo;
            const records = r.data.map((x) => ({
              ...x,
              timeText: x.signTime && x.signTime.$date ? formatTime(x.signTime.$date) : "",
            }));
            const myRecord = records.find((x) => x._openid === me?._openid) || null;
            this.setData({
              records,
              myRecord,
              countText: `共 ${records.length} 人报名`,
            });
          });
      })
      .catch(() => wx.showToast({ title: "志愿活动不存在", icon: "none" }));
  },

  // 同学在线报名
  doSign() {
    if (this.data.signing) return;
    this.setData({ signing: true });
    wx.cloud
      .callFunction({
        name: "api",
        data: { action: "joinVolunteer", data: { taskId: this.id } },
      })
      .then((res) => {
        if (res.result.ok) {
          wx.showToast({ title: "报名成功", icon: "success" });
          this.fetchAll();
        } else {
          wx.showModal({ title: "报名失败", content: res.result.msg, showCancel: false });
        }
      })
      .catch(() => wx.showToast({ title: "网络异常", icon: "none" }))
      .finally(() => this.setData({ signing: false }));
  },
});
