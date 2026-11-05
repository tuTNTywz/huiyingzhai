// 发布作业（班委）
Page({
  data: {
    title: "",
    desc: "",
    date: "",
    time: "23:59",
    attachments: [], // [{ name, size, tempPath }]
    minDate: "",
    submitting: false,
  },

  onLoad() {
    const now = new Date();
    const p = (n) => (n < 10 ? "0" + n : "" + n);
    this.setData({
      minDate: `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`,
    });
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

  // 从聊天记录中选择 PDF / Word 等文件
  chooseFile() {
    wx.chooseMessageFile({
      count: 5,
      type: "file",
      extension: ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt"],
      success: (res) => {
        const files = res.tempFiles.map((f) => ({
          name: f.name,
          size: f.size,
          tempPath: f.path,
        }));
        this.setData({ attachments: this.data.attachments.concat(files) });
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

  // 逐个上传附件到云存储
  uploadAttachments() {
    const tasks = this.data.attachments.map((f, i) => {
      const cloudPath = `homework/${Date.now()}_${i}_${f.name}`;
      return wx.cloud
        .uploadFile({ cloudPath, filePath: f.tempPath })
        .then((res) => ({ fileID: res.fileID, name: f.name }));
    });
    return Promise.all(tasks).catch((err) => {
      console.error(err);
      return Promise.reject("附件上传失败");
    });
  },

  submit() {
    if (!this.validate()) return;
    this.setData({ submitting: true });
    wx.showLoading({ title: "发布中…", mask: true });

    this.uploadAttachments()
      .then((attachments) => {
        const deadline = new Date(`${this.data.date}T${this.data.time}:00`).getTime();
        return wx.cloud.callFunction({
          name: "api",
          data: {
            action: "publishHomework",
            data: {
              title: this.data.title.trim(),
              desc: this.data.desc.trim(),
              deadline,
              attachments,
            },
          },
        });
      })
      .then((res) => {
        wx.hideLoading();
        if (res.result.ok) {
          wx.showToast({ title: "发布成功" });
          setTimeout(() => wx.navigateBack(), 800);
        } else {
          wx.showToast({ title: res.result.msg, icon: "none" });
        }
      })
      .catch((msg) => {
        wx.hideLoading();
        wx.showToast({ title: typeof msg === "string" ? msg : "发布失败", icon: "none" });
      })
      .finally(() => this.setData({ submitting: false }));
  },
});
