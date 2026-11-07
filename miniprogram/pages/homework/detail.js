// 作业详情 + 附件查看（文档打开 / 图片预览）
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
    wx.cloud
      .database()
      .collection("homeworks")
      .doc(this.id)
      .get()
      .then((res) => {
        const hw = res.data;
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
            createTimeText: hw.createTime && hw.createTime.$date
              ? formatTime(hw.createTime.$date) : "",
          },
          expired: deadlineMs < Date.now(),
        });
      })
      .catch(() => wx.showToast({ title: "作业不存在或已被删除", icon: "none" }));
  },

  downloadFile(e) {
    const { fileid, name, type } = e.currentTarget.dataset;
    if (!fileid || this.data.downloading) return;
    this.setData({ downloading: fileid });
    wx.showLoading({ title: "加载中…", mask: true });

    // 图片附件：换取临时链接后全屏预览
    if (type === "image") {
      wx.cloud
        .getTempFileURL({ fileList: [fileid] })
        .then((res) => {
          wx.hideLoading();
          const item = res.fileList && res.fileList[0];
          if (item && item.tempFileURL) {
            wx.previewImage({ urls: [item.tempFileURL] });
          } else {
            wx.showToast({ title: "图片加载失败", icon: "none" });
          }
        })
        .catch(() => {
          wx.hideLoading();
          wx.showToast({ title: "图片加载失败", icon: "none" });
        })
        .finally(() => this.setData({ downloading: "" }));
      return;
    }

    // 文档附件：下载后用微信内置文档预览打开，可另存 / 转发
    wx.cloud
      .downloadFile({ fileID: fileid })
      .then((res) => {
        wx.hideLoading();
        const ext = (name || "").split(".").pop().toLowerCase();
        wx.openDocument({
          filePath: res.tempFilePath,
          fileType: ext,
          showMenu: true,
          fail: () => {
            wx.saveFile({
              tempFilePath: res.tempFilePath,
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
