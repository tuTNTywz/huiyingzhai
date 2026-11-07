// 交流栏目：所有人可自由发帖（可带文件 / 照片附件）
const app = getApp();
const { formatTime, toDateMs } = require("../../utils/util");
const { requireRegistered } = require("../../utils/guard");
const A = require("../../utils/attachments");

Page({
  data: {
    content: "",
    list: [],
    loading: true,
    publishing: false,
    nickname: "同窗",
    attachments: [],
    uploadText: "",
    opening: false,
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
          attachments: A.decorate(p.attachments),
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
  noop() {},

  // ===== 发帖附件 =====
  pick(picker) {
    const remain = A.MAX_FILES - this.data.attachments.length;
    if (remain <= 0) {
      wx.showToast({ title: `最多添加 ${A.MAX_FILES} 个附件`, icon: "none" });
      return;
    }
    picker(remain).then((files) => {
      this.setData({ attachments: A.merge(this.data.attachments, files) });
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
    this.setData({ attachments: list });
  },

  openAttachment(e) {
    if (this.data.opening) return;
    this.setData({ opening: true });
    A.open(e.currentTarget.dataset).finally(() => this.setData({ opening: false }));
  },

  publish() {
    const content = this.data.content.trim();
    if (!content && !this.data.attachments.length) {
      wx.showToast({ title: "请写下内容或添加附件", icon: "none" });
      return;
    }
    if (this.data.publishing) return;
    this.setData({ publishing: true, uploadText: "准备上传…" });

    A.upload(this.data.attachments, "forum/posts", (text) =>
      this.setData({ uploadText: text })
    )
      .then((attachments) => {
        this.setData({ uploadText: "正在发布…" });
        return wx.cloud.callFunction({
          name: "api",
          data: { action: "createPost", data: { content, attachments } },
        });
      })
      .then((res) => {
        if (res.result.ok) {
          this.setData({ content: "", attachments: [] });
          wx.showToast({ title: "已发布", icon: "success" });
          this.fetchList();
        } else {
          wx.showToast({ title: res.result.msg, icon: "none" });
        }
      })
      .catch((msg) => {
        wx.showToast({ title: typeof msg === "string" ? msg : "发布失败", icon: "none" });
      })
      .finally(() => this.setData({ publishing: false, uploadText: "" }));
  },

  goDetail(e) {
    wx.navigateTo({ url: `/pages/forum/detail?id=${e.currentTarget.dataset.id}` });
  },

  onDelete(e) {
    const id = e.currentTarget.dataset.id;
    wx.showModal({
      title: "删除帖子",
      content: "确定删除该帖、全部回复及其附件吗？",
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
