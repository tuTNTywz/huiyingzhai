// 签到任务列表
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
      .collection("sign_tasks")
      .orderBy("createTime", "desc")
      .limit(20)
      .get()
      .then((res) => {
        const now = Date.now();
        const list = res.data.map((t) => {
          const start = toDateMs(t.startTime) || t.startTime;
          const end = toDateMs(t.endTime) || t.endTime;
          const status = now < start ? "pending" : now <= end ? "active" : "ended";
          return {
            ...t,
            status,
            endText: formatTime(end),
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
    wx.navigateTo({ url: "/pages/sign/create" });
  },

  goDetail(e) {
    wx.navigateTo({
      url: `/pages/sign/detail?id=${e.currentTarget.dataset.id}`,
    });
  },
});
