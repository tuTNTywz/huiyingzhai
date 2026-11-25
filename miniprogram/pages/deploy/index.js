// 部署控制台：仅管理员可进入，直接调用网页部署接口
const { BASE_URL } = require("../../config");

Page({
  data: {
    status: null,
    current: null,
    log: "",
    busy: false,
    statusText: "加载中",
    statusClass: "",
  },

  onShow() {
    this.loadStatus();
  },

  token() {
    return wx.getStorageSync("api_token") || "";
  },

  label(status) {
    if (!status) return "暂无部署";
    if (status.state === "running") return "部署中";
    if (status.state === "success") return "已完成";
    if (status.state === "failed") return "部署失败";
    return "未知";
  },

  statusClass(status) {
    if (!status) return "";
    return status.state || "";
  },

  loadStatus() {
    const token = this.token();
    if (!token) {
      wx.showToast({ title: "登录已失效，请重新进入", icon: "none" });
      return;
    }
    wx.request({
      url: BASE_URL + "/me/api/app/deploy/status",
      method: "GET",
      header: { Authorization: "Bearer " + token },
      success: (res) => {
        const data = res.data || {};
        if (res.statusCode !== 200 || !data.ok) {
          wx.showToast({ title: data.message || "读取部署状态失败", icon: "none" });
          this.setData({ busy: false, statusText: "读取失败" });
          return;
        }
        const status = data.status || null;
        const running = !!(status && status.state === "running");
        this.setData({
          status,
          current: data.current || null,
          log: data.log || "",
          busy: running,
          statusText: this.label(status),
          statusClass: this.statusClass(status),
        });
        if (running) {
          setTimeout(() => this.loadStatus(), 2500);
        }
      },
      fail: () => {
        wx.showToast({ title: "网络错误，请重试", icon: "none" });
        this.setData({ busy: false, statusText: "网络错误" });
      },
    });
  },

  onDeploy() {
    if (this.data.busy) return;
    wx.showModal({
      title: "部署最新代码",
      content: "确定从 GitHub 拉取最新代码并重启服务吗？失败会自动回滚。",
      success: (res) => {
        if (res.confirm) this.doDeploy();
      },
    });
  },

  doDeploy() {
    const token = this.token();
    if (!token) {
      wx.showToast({ title: "登录已失效，请重新进入", icon: "none" });
      return;
    }
    this.setData({ busy: true, statusText: "提交中…" });
    wx.request({
      url: BASE_URL + "/me/api/app/deploy",
      method: "POST",
      header: {
        Authorization: "Bearer " + token,
        "Content-Type": "application/json",
      },
      data: {},
      success: (res) => {
        const data = res.data || {};
        if (res.statusCode >= 200 && res.statusCode < 300 && data.ok) {
          wx.showToast({ title: "已提交部署" });
          this.loadStatus();
        } else {
          wx.showToast({ title: data.message || "提交部署失败", icon: "none" });
          this.setData({ busy: false });
        }
      },
      fail: () => {
        wx.showToast({ title: "网络错误，请重试", icon: "none" });
        this.setData({ busy: false });
      },
    });
  },
});
