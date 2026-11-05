// 作业详情 + 附件下载
const { formatTime, toDateMs } = require("../../utils/util");

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
        this.setData({
          homework: {
            ...hw,
            deadlineText: formatTime(deadlineMs),
            createTimeText: hw.createTime && hw.createTime.$date
              ? formatTime(hw.createTime.$date) : "",
          },
          expired: deadlineMs < Date.now(),
        });
      })
      .catch(() => wx.showToast({ title: "作业不存在或已被删除", icon: "none" }));
  },

  // 下载并预览附件
  downloadFile(e) {
    const fileID = e.currentTarget.dataset.fileid;
    const name = e.currentTarget.dataset.name || "";
    if (this.data.downloading) return;
    this.setData({ downloading: fileID });
    wx.showLoading({ title: "下载中…", mask: true });

    wx.cloud
      .downloadFile({ fileID })
      .then((res) => {
        const ext = name.split(".").pop().toLowerCase();
        wx.openDocument({
          filePath: res.tempFilePath,
          fileType: ext,
          showMenu: true,
          fail: () => {
            // 无法预览时保存到本地文件系统提示
            wx.saveFile({
              tempFilePath: res.tempFilePath,
              success: () => wx.showToast({ title: "已保存，可在文件管理中查看" }),
              fail: () => wx.showToast({ title: "该类型暂不支持预览", icon: "none" }),
            });
          },
        });
      })
      .catch(() => wx.showToast({ title: "下载失败", icon: "none" }))
      .finally(() => {
        wx.hideLoading();
        this.setData({ downloading: "" });
      });
  },
});
