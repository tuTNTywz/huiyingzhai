// 志愿活动列表（发布在网页上传页，班委/管理员可用）
const app = getApp();
const api = require("../../utils/api");
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
    api
      .call("listVolunteers")
      .then((res) => {
        if (!res.ok) throw res.msg;
        const now = Date.now();
        const list = (res.list || []).map((a) => {
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

  goDetail(e) {
    wx.navigateTo({ url: `/pages/volunteer/detail?id=${e.currentTarget.dataset.id}` });
  },

  onDelete(e) {
    const id = e.currentTarget.dataset.id;
    wx.showModal({
      title: "删除志愿活动",
      content: "确定删除该志愿活动吗？",
      confirmColor: "#b5543c",
      success: (res) => {
        if (!res.confirm) return;
        api
          .call("deleteActivity", { id })
          .then((r) => {
            if (r.ok) {
              wx.showToast({ title: "已删除" });
              this.fetchList();
            } else {
              wx.showToast({ title: r.msg, icon: "none" });
            }
          })
          .catch(() => wx.showToast({ title: "删除失败", icon: "none" }));
      },
    });
  },
});
