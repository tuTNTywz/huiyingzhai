// 作业首页：九个学科板块入口 + 班级作业列表
const app = getApp();
const { formatTime, toDateMs } = require("../../utils/util");
const { requireRegistered } = require("../../utils/guard");
const { SUBJECTS } = require("../../utils/subjects");

Page({
  data: {
    subjects: SUBJECTS,
    list: [],
    loading: true,
    isCommittee: false,
    now: 0,
  },

  onShow() {
    if (!requireRegistered()) return;
    const refresh = () => this.setData({ isCommittee: app.isCommittee() });
    if (app.globalData.userInfo) refresh();
    else app.userReadyCallback = refresh;
    this.fetchList();
    this.timer = setInterval(() => this.setData({ now: Date.now() }), 30000);
    this.setData({ now: Date.now() });
  },

  onHide() {
    clearInterval(this.timer);
  },

  onUnload() {
    clearInterval(this.timer);
  },

  goSubject(e) {
    wx.navigateTo({
      url: `/pages/subject/index?key=${e.currentTarget.dataset.key}`,
    });
  },

  fetchList() {
    wx.showLoading({ title: "载入中…" });
    wx.cloud
      .database()
      .collection("homeworks")
      .orderBy("createTime", "desc")
      .limit(20)
      .get()
      .then((res) => {
        const now = Date.now();
        const list = res.data.map((item) => {
          const deadlineMs = toDateMs(item.deadline) || item.deadline;
          return { ...item, deadlineMs: deadlineMs, deadlineText: formatTime(deadlineMs), expired: deadlineMs < now };
        });
        this.setData({ list, loading: false, now });
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
    wx.navigateTo({ url: "/pages/homework/create" });
  },

  goDetail(e) {
    wx.navigateTo({
      url: `/pages/homework/detail?id=${e.currentTarget.dataset.id}`,
    });
  },

  // 班委长按删除
  onDelete(e) {
    const id = e.currentTarget.dataset.id;
    wx.showModal({
      title: "删除作业",
      content: "确定删除该作业及其附件吗？",
      confirmColor: "#b5543c",
      success: (res) => {
        if (!res.confirm) return;
        wx.cloud.callFunction({
          name: "api",
          data: { action: "deleteHomework", data: { id } },
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
