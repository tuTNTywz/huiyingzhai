// 作业详情 + 附件查看（文档打开 / 图片预览）
const api = require("../../utils/api");
const { formatTime, formatSize, toDateMs } = require("../../utils/util");

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
        const attachments = (hw.attachments || []).map((a) => ({
          ...a,
          sizeText: a.size ? formatSize(a.size) : "",
        }));
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

  downloadFile(e) {
    const { fileid, name, type } = e.currentTarget.dataset;
    if (!fileid || this.data.downloading) return;
    this.setData({ downloading: fileid });
    wx.showLoading({ title: "加载中…", mask: true });

    // 图片附件：直接使用完整 URL 全屏预览
    if (type === "image") {
      wx.hideLoading();
      wx.previewImage({
        urls: [api.fileUrl(fileid)],
        fail: () => wx.showToast({ title: "图片加载失败", icon: "none" }),
        complete: () => this.setData({ downloading: "" }),
      });
      return;
    }

    // 文档附件：下载后用微信内置文档预览打开，可另存 / 转发
    api
      .download(fileid)
      .then((tempFilePath) => {
        wx.hideLoading();
        const ext = (name || "").split(".").pop().toLowerCase();
        wx.openDocument({
          filePath: tempFilePath,
          fileType: ext,
          showMenu: true,
          fail: () => {
            wx.saveFile({
              tempFilePath,
              success: () => wx.showToast({ title: "已保存，可在文件管理中查看" }),
              fail: () => wx.showToast({ title: "该类型暂不支持预览", icon: "none" }),
            });
          },
        });
      })
      .catch(() => {
        wx.hideLoading();
        wx.showToast({ title: "下载失败", icon: "none" });
      })
      .finally(() => this.setData({ downloading: "" }));
  },
});
