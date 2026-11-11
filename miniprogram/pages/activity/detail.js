// 活动详情
const app = getApp();
const api = require("../../utils/api");
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
    api
      .call("getActivity", { id: this.id })
      .then((res) => {
        if (!res.ok) throw res.msg;
        const a = res.item;
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
      .catch((msg) =>
        wx.showToast({
          title: typeof msg === "string" ? msg : "活动不存在或已被删除",
          icon: "none",
        })
      );
  },

  onDelete() {
    wx.showModal({
      title: "删除活动",
      content: "确定删除该活动吗？",
      confirmColor: "#b5543c",
      success: (res) => {
        if (!res.confirm) return;
        api
          .call("deleteActivity", { id: this.id })
          .then((r) => {
            if (r.ok) {
              wx.showToast({ title: "已删除" });
              setTimeout(() => wx.navigateBack(), 600);
            } else {
              wx.showToast({ title: r.msg, icon: "none" });
            }
          })
          .catch(() => wx.showToast({ title: "删除失败", icon: "none" }));
      },
    });
  },
});
