// 作业详情 + 附件查看（统一走 utils/attachments，照片本地预览 / 文档内置打开）
const api = require("../../utils/api");
const { formatTime, toDateMs } = require("../../utils/util");
const A = require("../../utils/attachments");

Page({
  data: {
    homework: null,
    expired: false,
    downloading: "",
  },

  onLoad(options) {
    this.id = options.id;
    this.fetchDetail();
  },

  fetchDetail() {
    api
      .call("getHomework", { id: this.id })
      .then((res) => {
        if (!res.ok) throw res.msg;
        const hw = res.item;
        const deadlineMs = toDateMs(hw.deadline) || hw.deadline;
        // A.decorate 会补齐 type（历史附件缺失时按文件名兜底）与 sizeText
        const attachments = A.decorate(hw.attachments);
        this.setData({
          homework: {
            ...hw,
            attachments,
            deadlineText: formatTime(deadlineMs),
            createTimeText: hw.createTime ? formatTime(toDateMs(hw.createTime)) : "",
          },
          expired: deadlineMs < Date.now(),
        });
      })
      .catch((msg) =>
        wx.showToast({
          title: typeof msg === "string" ? msg : "作业不存在或已被删除",
          icon: "none",
        })
      );
  },

  // 照片：下载到本地临时文件后全屏预览（云存储临时链接真机上常无法显示）
  // 文档：下载后用微信内置预览打开，可另存 / 转发
  downloadFile(e) {
    const file = e.currentTarget.dataset;
    if (!file.fileid || this.data.downloading) return;
    this.setData({ downloading: file.fileid });
    A.open(file).finally(() => this.setData({ downloading: "" }));
  },
});
