// 注册守卫：未注册用户可浏览，但写操作由云函数拦截
// 首次登录由 app.js 引导至「我的」页弹出注册窗口
let tipShown = false;

function requireRegistered() {
  const app = getApp();
  if (app && app.globalData.userInfo && app.globalData.registered === false && !tipShown) {
    tipShown = true;
    wx.showToast({ title: "请先到「我的」完成登记", icon: "none" });
  }
  return true;
}

module.exports = { requireRegistered };
