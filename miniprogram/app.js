// app.js
App({
  globalData: {
    // env 参数决定小程序发起的云开发调用请求到哪个云环境
    // 请填入环境 ID（微信开发者工具 -> 云开发 -> 环境设置 中查看）
    // 留空则使用默认环境
    env: "cloud1-d6ghclqds0c2bf34d",
    userInfo: null, // { _id, _openid, name, studentId, className, position, nickname, role, registered }
    registered: null, // null=登录中 / false=未注册 / true=已注册
    needRegisterModal: false, // 首次登录自动在「我的」弹出注册窗口
  },

  onLaunch: function () {
    if (!wx.cloud) {
      console.error("请使用 2.2.3 或以上的基础库以使用云能力");
      return;
    }
    // env 为空时不传该参数，使用云开发的默认环境（单环境项目可留空）
    const cloudOptions = { traceUser: true };
    if (this.globalData.env) cloudOptions.env = this.globalData.env;
    try {
      wx.cloud.init(cloudOptions);
    } catch (e) {
      console.error("云开发初始化失败，请检查环境 ID", e);
    }
    this.ensureUser();
  },

  // 云函数自动建档并返回注册状态；首次登录引导至「我的」注册窗口
  ensureUser() {
    wx.cloud.callFunction({
      name: "api",
      data: { action: "login" },
      success: (res) => {
        const r = res.result || {};
        if (r.ok) {
          this.globalData.userInfo = r.user;
          this.globalData.registered = !!r.registered;
          if (!r.registered) this.globalData.needRegisterModal = true;
          if (this.userReadyCallback) this.userReadyCallback(r.user);
          if (!r.registered) {
            wx.switchTab({ url: "/pages/mine/index" });
          }
        } else {
          // 云函数返回业务错误时不再静默（否则界面会一直停在「未登录」状态）
          console.error("登录失败", r.msg);
          wx.showToast({ title: r.msg || "登录失败，请稍后重试", icon: "none" });
          if (this.userReadyCallback) this.userReadyCallback(null);
        }
      },
      fail: (err) => {
        console.error("登录失败", err);
        wx.showToast({ title: "登录失败，请检查网络或云函数是否已部署", icon: "none" });
        if (this.userReadyCallback) this.userReadyCallback(null);
      },
    });
  },

  // 重新拉取最新用户资料（重新登记后同步展示）
  refreshUser() {
    return wx.cloud
      .callFunction({ name: "api", data: { action: "login" } })
      .then((res) => {
        if (res.result && res.result.ok) {
          this.globalData.userInfo = res.result.user;
          this.globalData.registered = !!res.result.registered;
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
