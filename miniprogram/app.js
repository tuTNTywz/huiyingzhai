// app.js
const api = require("./utils/api");

App({
  globalData: {
    userInfo: null, // { _id, _openid, name, studentId, className, position, nickname, role, registered }
    registered: null, // null=登录中 / false=未注册 / true=已注册
    needRegisterModal: false, // 首次登录自动在「我的」弹出注册窗口
  },

  onLaunch: function () {
    if (/example\.com/.test(api.BASE_URL)) {
      console.warn("请先在 miniprogram/config.js 中配置后端域名 BASE_URL");
    }
    this.ensureUser();
  },

  // 自建后端自动建档并返回注册状态；首次登录引导至「我的」注册窗口
  ensureUser() {
    api
      .login()
      .then((r) => {
        this.globalData.userInfo = r.user || null;
        this.globalData.registered = !!r.registered;
        if (!r.registered) this.globalData.needRegisterModal = true;
        if (this.userReadyCallback) this.userReadyCallback(r.user || null);
        if (!r.registered) {
          wx.switchTab({ url: "/pages/mine/index" });
        }
      })
      .catch((err) => {
        console.error("登录失败", err);
        wx.showToast({
          title: typeof err === "string" ? err : "登录失败，请稍后重试",
          icon: "none",
        });
        if (this.userReadyCallback) this.userReadyCallback(null);
      });
  },

  // 重新拉取最新用户资料（重新登记后同步展示）
  refreshUser() {
    return api
      .call("login")
      .then((r) => {
        if (r && r.ok && r.user) {
          this.globalData.userInfo = r.user;
          this.globalData.registered = !!r.registered;
        }
        return this.globalData.userInfo;
      })
      .catch(() => this.globalData.userInfo);
  },

  // 登记 / 修改资料成功后更新全局用户信息
  updateUser(user) {
    this.globalData.userInfo = user;
    this.globalData.registered = true;
    this.globalData.needRegisterModal = false;
    if (this.userReadyCallback) this.userReadyCallback(user);
  },

  // 判断是否班委（管理员同样具备班委权限）
  isCommittee() {
    const r = this.globalData.userInfo && this.globalData.userInfo.role;
    return r === "committee" || r === "admin";
  },

  // 判断是否管理员
  isAdmin() {
    return this.globalData.userInfo && this.globalData.userInfo.role === "admin";
  },
});
