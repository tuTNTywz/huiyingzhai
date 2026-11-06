// 帖子详情：所有人可自由回复
const app = getApp();
const { formatTime, toDateMs } = require("../../utils/util");

Page({
  data: {
    post: null,
    replies: [],
    content: "",
    sending: false,
    nickname: "同窗",
  },

  onLoad(options) {
    this.id = options.id;
  },

  onShow() {
    const refresh = () => {
      if (app.globalData.userInfo) {
        this.setData({ nickname: app.globalData.userInfo.nickname || "同窗" });
      }
    };
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
    db.collection("posts")
      .doc(this.id)
      .get()
      .then((res) => {
        const p = res.data;
        this.setData({
          post: {
            ...p,
            timeText: formatTime(toDateMs(p.createTime)),
            avatarText: (p.nickname || "同").charAt(0),
            isCommittee: p.role === "committee" || p.role === "admin",
          },
        });
        return db
          .collection("post_replies")
          .where({ postId: this.id })
          .orderBy("createTime", "asc")
          .limit(100)
          .get()
          .then((r) => {
            this.setData({
              replies: r.data.map((x) => ({
                ...x,
                timeText: formatTime(toDateMs(x.createTime)),
                avatarText: (x.nickname || "同").charAt(0),
              })),
            });
          });
      })
      .catch(() => wx.showToast({ title: "帖子不存在或已被删除", icon: "none" }));
  },

  onInput(e) {
    this.setData({ content: e.detail.value });
  },

  sendReply() {
    const content = this.data.content.trim();
    if (!content) {
      wx.showToast({ title: "请先写下回复", icon: "none" });
      return;
    }
    this.setData({ sending: true });
    wx.cloud
      .callFunction({
        name: "api",
        data: { action: "createReply", data: { postId: this.id, content } },
      })
      .then((res) => {
        if (res.result.ok) {
          this.setData({ content: "" });
          this.fetchAll();
        } else {
          wx.showToast({ title: res.result.msg, icon: "none" });
        }
      })
      .catch(() => wx.showToast({ title: "回复失败", icon: "none" }))
      .finally(() => this.setData({ sending: false }));
  },
});
