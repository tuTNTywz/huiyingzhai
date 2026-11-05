// 活动详情
const app = getApp();
const { formatTime, toDateMs } = require("../../utils/util");

Page({
  data: {
    activity: null,
    ended: false,
    isCommittee: false,
  },

  onLoad(options) {
    this.id = options.id;
  },

  onShow() {
    const refresh = () => this.setData({ isCommittee: app.isCommittee() });
    if (app.globalData.userInfo) refresh();
    else app.userReadyCallback = refresh;
    this.fetchDetail();
  },

  fetchDetail() {
    wx.cloud
      .database()
      .collection("activities")
      .doc(this.id)
      .get()
      .then((res) => {
        const a = res.data;
        const ms = toDateMs(a.activityTime) || a.activityTime;
        this.setData({
          activity: {
            ...a,
            timeText: formatTime(ms),
            createTimeText: formatTime(toDateMs(a.createTime)),
          },
          ended: ms < Date.now(),
        });
      })
      .catch(() => wx.showToast({ title: "活动不存在或已被删除", icon: "none" }));
  },

  onDelete() {
    wx.showModal({
      title: "删除活动",
      content: "确定删除该活动吗？",
      confirmColor: "#b5543c",
      success: (res) => {
        if (!res.confirm) return;
        wx.cloud.callFunction({
          name: "api",
          data: { action: "deleteActivity", data: { id: this.id } },
          success: (r) => {
            if (r.result.ok) {
              wx.showToast({ title: "已删除" });
              setTimeout(() => wx.navigateBack(), 600);
            } else {
              wx.showToast({ title: r.result.msg, icon: "none" });
            }
          },
        });
      },
    });
  },
});
