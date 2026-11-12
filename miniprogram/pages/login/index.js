// 登录页：可选择绑定微信，或暂不绑定先浏览
const app = getApp();

Page({
  data: {
    bound: false,
    registered: false,
    nickname: "同窗",
    busy: false,
    fromMine: false,
  },

  onLoad(options) {
    this.setData({ fromMine: options.from === "mine" });
    const ready = (u) => {
      const info = u || app.globalData.userInfo || {};
      this.setData({
        bound: !!info.wechatBound,
        registered: !!info.registered,
        nickname: info.name || info.nickname || "同窗",
      });
      // 已绑定且已登记的用户冷启动直接进入；从「我的」进入则停留查看绑定状态
      if (!this.data.fromMine && info.wechatBound && info.registered) {
        this.enter();
      }
    };
    if (app.globalData.userInfo) ready(app.globalData.userInfo);
    else app.userReadyCallback = ready;
  },

  bindWechat() {
    if (this.data.bound) return this.enter();
    if (this.data.busy) return;
    this.setData({ busy: true });
    wx.showLoading({ title: "绑定中…", mask: true });

    wx.cloud
      .callFunction({ name: "api", data: { action: "bindWechat" } })
      .then((res) => {
        wx.hideLoading();
        const r = res.result || {};
        if (!r.ok) {
          wx.showToast({ title: r.msg || "绑定失败", icon: "none" });
          return;
        }
        const u = r.user || {};
        app.globalData.userInfo = { ...(app.globalData.userInfo || {}), ...u };
        app.globalData.registered = !!r.registered;
        this.setData({
          bound: true,
          registered: app.globalData.registered,
          nickname: u.name || u.nickname || "同窗",
        });
        wx.showToast({ title: "已绑定微信" });
        setTimeout(() => this.enter(), 700);
      })
      .catch(() => {
        wx.hideLoading();
        wx.showToast({ title: "网络异常，请重试", icon: "none" });
      })
      .finally(() => this.setData({ busy: false }));
  },

  // 暂不绑定：直接进入首页浏览（写操作仍由云函数按登记状态拦截）
  skip() {
    if (this.data.fromMine) {
      wx.navigateBack();
      return;
    }
    wx.switchTab({ url: "/pages/homework/index" });
  },

  enter() {
    // 从「我的」进入时，操作完成后返回原页面
    if (this.data.fromMine) {
      wx.navigateBack();
      return;
    }
    if (app.globalData.registered === false) {
      // 首次使用：绑定后引导完成学籍登记（「我的」页会自动弹出登记窗口）
      app.globalData.needRegisterModal = true;
      wx.switchTab({ url: "/pages/mine/index" });
    } else {
      wx.switchTab({ url: "/pages/homework/index" });
    }
  },
});
