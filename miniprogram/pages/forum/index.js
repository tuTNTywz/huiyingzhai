// 交流栏目：所有人可自由发帖
const app = getApp();
const { formatTime, toDateMs } = require("../../utils/util");
const { requireRegistered } = require("../../utils/guard");

Page({
  data: {
    content: "",
    list: [],
    loading: true,
    publishing: false,
    nickname: "同窗",
  },

  onShow() {
    if (!requireRegistered()) return;
    const refresh = () => {
      if (app.globalData.userInfo) {
        this.setData({ nickname: app.globalData.userInfo.nickname || "同窗" });
      }
    };
    if (app.globalData.userInfo) refresh();
    else app.userReadyCallback = refresh;
    this.fetchList();
  },

  fetchList() {
    wx.showLoading({ title: "载入中…" });
    wx.cloud
      .database()
      .collection("posts")
      .orderBy("createTime", "desc")
      .limit(20)
      .get()
      .then((res) => {
        const list = res.data.map((p) => ({
          ...p,
          timeText: formatTime(toDateMs(p.createTime)),
          avatarText: (p.nickname || "同").charAt(0),
          isCommittee: p.role === "committee" || p.role === "admin",
        }));
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

  onInput(e) {
    this.setData({ content: e.detail.value });
  },

  publish() {
    const content = this.data.content.trim();
    if (!content) {
      wx.showToast({ title: "请先写下内容", icon: "none" });
      return;
    }
    this.setData({ publishing: true });
    wx.cloud
      .callFunction({
        name: "api",
        data: { action: "createPost", data: { content } },
      })
      .then((res) => {
        if (res.result.ok) {
          this.setData({ content: "" });
          wx.showToast({ title: "已发布", icon: "success" });
          this.fetchList();
        } else {
          wx.showToast({ title: res.result.msg, icon: "none" });
        }
      })
      .catch(() => wx.showToast({ title: "发布失败", icon: "none" }))
      .finally(() => this.setData({ publishing: false }));
  },

  goDetail(e) {
    wx.navigateTo({ url: `/pages/forum/detail?id=${e.currentTarget.dataset.id}` });
  },

  onDelete(e) {
    const id = e.currentTarget.dataset.id;
    wx.showModal({
      title: "删除帖子",
      content: "确定删除该帖及其回复吗？",
      confirmColor: "#b5543c",
      success: (res) => {
        if (!res.confirm) return;
        wx.cloud.callFunction({
          name: "api",
          data: { action: "deletePost", data: { id } },
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
