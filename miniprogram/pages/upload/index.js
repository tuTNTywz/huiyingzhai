// 汇英斋数据上传：在 web-view 中打开网页上传页，自动带上小程序登录 token
const { BASE_URL } = require("../../config");

Page({
  data: {
    url: "",
  },

  onLoad() {
    const token = wx.getStorageSync("api_token") || "";
    if (!token) {
      wx.showToast({ title: "登录已失效，请重新进入", icon: "none" });
      return;
    }
    this.setData({
      // token 放在 URL fragment 中，不会进入 Nginx 访问日志
      url: BASE_URL + "/upload/#ticket=" + encodeURIComponent(token),
    });
  },
});
