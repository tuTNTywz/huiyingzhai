// 帖子详情：所有人可自由回复（可带文件 / 照片附件）
const app = getApp();
const api = require("../../utils/api");
const { formatTime, toDateMs } = require("../../utils/util");
const A = require("../../utils/attachments");

Page({
  data: {
    post: null,
    replies: [],
    content: "",
    sending: false,
    nickname: "同窗",
    attachments: [],
    attachBadge: "",
    showAttachPanel: false,
    uploadText: "",
    opening: false,
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
    api
      .call("getPost", { id: this.id })
      .then((res) => {
        if (!res.ok) throw res.msg;
        const p = res.item;
        this.setData({
          post: {
            ...p,
            attachments: A.decorate(p.attachments),
            timeText: formatTime(toDateMs(p.createTime)),
            avatarText: (p.nickname || "同").charAt(0),
            isCommittee: p.role === "committee" || p.role === "admin",
          },
        });
        return api.call("listReplies", { postId: this.id }).then((r) => {
          if (!r.ok) throw r.msg;
          this.setData({
            replies: (r.list || []).map((x) => ({
              ...x,
              attachments: A.decorate(x.attachments),
              timeText: formatTime(toDateMs(x.createTime)),
              avatarText: (x.nickname || "同").charAt(0),
            })),
          });
        });
      })
      .catch((msg) =>
        wx.showToast({
          title: typeof msg === "string" ? msg : "帖子不存在或已被删除",
          icon: "none",
        })
      );
  },

  onInput(e) {
    this.setData({ content: e.detail.value });
  },

  // ===== 回复附件 =====
  toggleAttachPanel() {
    this.setData({ showAttachPanel: !this.data.showAttachPanel });
  },

  setAttachments(list) {
    this.setData({
      attachments: list,
      attachBadge: list.length ? String(list.length) : "",
    });
  },

  pick(picker) {
    const remain = A.MAX_FILES - this.data.attachments.length;
    if (remain <= 0) {
      wx.showToast({ title: `最多添加 ${A.MAX_FILES} 个附件`, icon: "none" });
      return;
    }
    picker(remain).then((files) => {
      this.setAttachments(A.merge(this.data.attachments, files));
    });
  },
  chooseFile() {
    this.pick(A.pickFiles);
  },
  chooseImage() {
    this.pick(A.pickImages);
  },
  removeFile(e) {
    const list = this.data.attachments.slice();
    list.splice(e.currentTarget.dataset.index, 1);
    this.setAttachments(list);
  },

  openAttachment(e) {
    if (this.data.opening) return;
    this.setData({ opening: true });
    A.open(e.currentTarget.dataset).finally(() => this.setData({ opening: false }));
  },

  sendReply() {
    const content = this.data.content.trim();
    if (!content && !this.data.attachments.length) {
      wx.showToast({ title: "请写下回复或添加附件", icon: "none" });
      return;
    }
    if (this.data.sending) return;
    this.setData({ sending: true, uploadText: "准备上传…" });

    A.upload(this.data.attachments, `forum/replies/${this.id}`, (text) =>
      this.setData({ uploadText: text })
    )
      .then((attachments) => {
        this.setData({ uploadText: "正在发送…" });
        return api.call("createReply", { postId: this.id, content, attachments });
      })
      .then((res) => {
        if (res.ok) {
          this.setData({ content: "", showAttachPanel: false });
          this.setAttachments([]);
          this.fetchAll();
        } else {
          wx.showToast({ title: res.msg, icon: "none" });
        }
      })
      .catch((msg) => {
        wx.showToast({ title: typeof msg === "string" ? msg : "回复失败", icon: "none" });
      })
      .finally(() => this.setData({ sending: false, uploadText: "" }));
  },
});
