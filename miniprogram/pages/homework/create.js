// 发布作业（班委 / 管理员）：支持上传文档与图片附件
const app = getApp();
const api = require("../../utils/api");
const { formatSize } = require("../../utils/util");

const MAX_FILES = 5;
const MAX_SIZE = 20 * 1024 * 1024;
const DOC_EXT = ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt"];

Page({
  data: {
    title: "",
    desc: "",
    date: "",
    time: "23:59",
    attachments: [], // [{ name, size, sizeText, tempPath, type }]
    minDate: "",
    submitting: false,
    uploadText: "",
  },

  onLoad() {
    const now = new Date();
    const p = (n) => (n < 10 ? "0" + n : "" + n);
    this.setData({
      minDate: `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`,
    });

    // 仅班委 / 管理员可进入发布页
    const check = () => {
      if (app.isCommittee()) return;
      wx.showModal({
        title: "权限不足",
        content: "仅班委或管理员可发布作业",
        showCancel: false,
        success: () => wx.navigateBack(),
      });
    };
    if (app.globalData.userInfo) check();
    else app.userReadyCallback = check;
  },

  onTitle(e) {
    this.setData({ title: e.detail.value });
  },
  onDesc(e) {
    this.setData({ desc: e.detail.value });
  },
  onDate(e) {
    this.setData({ date: e.detail.value });
  },
  onTime(e) {
    this.setData({ time: e.detail.value });
  },

  getRemain() {
    return MAX_FILES - this.data.attachments.length;
  },

  addFiles(files) {
    const list = this.data.attachments.slice();
    files.forEach((f) => {
      if (list.length >= MAX_FILES) return;
      if (f.size > MAX_SIZE) {
        wx.showToast({ title: `${f.name} 超过 20MB`, icon: "none" });
        return;
      }
      list.push({ ...f, sizeText: formatSize(f.size) });
    });
    if (files.length && list.length === this.data.attachments.length) {
      wx.showToast({ title: "未添加，请检查数量或大小", icon: "none" });
    }
    this.setData({ attachments: list });
  },

  // 从微信聊天记录选择 PDF / Word 等文档
  chooseFile() {
    const remain = this.getRemain();
    if (remain <= 0) {
      wx.showToast({ title: `最多添加 ${MAX_FILES} 个附件`, icon: "none" });
      return;
    }
    wx.chooseMessageFile({
      count: remain,
      type: "file",
      extension: DOC_EXT,
      success: (res) => {
        this.addFiles(
          res.tempFiles.map((f) => ({
            name: f.name,
            size: f.size,
            tempPath: f.path,
            type: "file",
          }))
        );
      },
    });
  },

  // 从相册或拍照添加图片附件
  chooseImage() {
    const remain = this.getRemain();
    if (remain <= 0) {
      wx.showToast({ title: `最多添加 ${MAX_FILES} 个附件`, icon: "none" });
      return;
    }
    wx.chooseMedia({
      count: remain,
      mediaType: ["image"],
      sourceType: ["album", "camera"],
      success: (res) => {
        this.addFiles(
          res.tempFiles.map((f, i) => ({
            name: `图片_${Date.now()}_${i + 1}.jpg`,
            size: f.size,
            tempPath: f.tempFilePath,
            type: "image",
          }))
        );
      },
    });
  },

  removeFile(e) {
    const idx = e.currentTarget.dataset.index;
    const list = this.data.attachments.slice();
    list.splice(idx, 1);
    this.setData({ attachments: list });
  },

  validate() {
    if (!this.data.title.trim()) {
      wx.showToast({ title: "请填写作业标题", icon: "none" });
      return false;
    }
    if (!this.data.date) {
      wx.showToast({ title: "请选择截止时间", icon: "none" });
      return false;
    }
    return true;
  },

  // 顺序上传附件到服务器；中途失败由 api.upload 清理已上传文件
  uploadAttachments() {
    return api.upload(this.data.attachments, "homework", (text) =>
      this.setData({ uploadText: text })
    );
  },

  submit() {
    if (!this.validate()) return;
    this.setData({ submitting: true, uploadText: "准备上传…" });

    this.uploadAttachments()
      .then((attachments) => {
        this.setData({ uploadText: "正在发布…" });
        const deadline = new Date(`${this.data.date}T${this.data.time}:00`).getTime();
        return api.call("publishHomework", {
          title: this.data.title.trim(),
          desc: this.data.desc.trim(),
          deadline,
          attachments,
        });
      })
      .then((res) => {
        if (res.ok) {
          wx.showToast({ title: "发布成功" });
          setTimeout(() => wx.navigateBack(), 800);
        } else {
          wx.showToast({ title: res.msg, icon: "none" });
        }
      })
      .catch((msg) => {
        wx.showToast({ title: typeof msg === "string" ? msg : "发布失败", icon: "none" });
      })
      .finally(() => this.setData({ submitting: false, uploadText: "" }));
  },
});
