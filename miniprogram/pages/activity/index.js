// 活动列表（仅班委可发布）
const app = getApp();
const { formatTime, toDateMs } = require("../../utils/util");
const { requireRegistered } = require("../../utils/guard");

Page({
  data: {
    list: [],
    loading: true,
    isCommittee: false,
  },

  onShow() {
    if (!requireRegistered()) return;
    const refresh = () => this.setData({ isCommittee: app.isCommittee() });
    if (app.globalData.userInfo) refresh();
    else app.userReadyCallback = refresh;
    this.fetchList();
  },

  fetchList() {
    wx.showLoading({ title: "载入中…" });
    wx.cloud
      .database()
      .collection("activities")
      .orderBy("activityTime", "desc")
      .limit(20)
      .get()
      .then((res) => {
        const now = Date.now();
        const list = res.data.map((a) => {
          const activityMs = toDateMs(a.activityTime) || a.activityTime;
          return {
            ...a,
            timeText: formatTime(activityMs),
            ended: activityMs < now,
          };
        });
        this.setData({ list, loading: false });
      })
      .catch(() => {
        this.setData({ loading: false });
        wx.showToast({ title: "加载失败", icon: "none" });
      })
      .finally(() => wx.hideLoading());
  },

  onPullDownRefresh() {
    this.fetchList();
    wx.stopPullDownRefresh();
  },

  goCreate() {
    wx.navigateTo({ url: "/pages/activity/create" });
  },

  goDetail(e) {
    wx.navigateTo({ url: `/pages/activity/detail?id=${e.currentTarget.dataset.id}` });
  },

  onDelete(e) {
    const id = e.currentTarget.dataset.id;
    wx.showModal({
      title: "删除活动",
      content: "确定删除该活动吗？",
      confirmColor: "#b5543c",
      success: (res) => {
        if (!res.confirm) return;
        wx.cloud.callFunction({
          name: "api",
          data: { action: "deleteActivity", data: { id } },
          success: (r) => {
            if (r.result.ok) {
              wx.showToast({ title: "已删除" });
              this.fetchList();
            } else {
              wx.showToast({ title: r.result.msg, icon: "none" });
            }
          },
        });
      },
    });
  },
});
