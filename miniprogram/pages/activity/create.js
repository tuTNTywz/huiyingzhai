// 发布活动（仅班委）
const app = getApp();

Page({
  data: {
    title: "",
    place: "",
    content: "",
    date: "",
    time: "09:00",
    minDate: "",
    submitting: false,
  },

  onLoad() {
    const now = new Date();
    const p = (n) => (n < 10 ? "0" + n : "" + n);
    this.setData({
      minDate: `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`,
    });
    const check = () => {
      if (app.isCommittee()) return;
      wx.showModal({
        title: "权限不足",
        content: "仅班委职位可发布活动，请到「我的」核对登记资料",
        showCancel: false,
        success: () => wx.navigateBack(),
      });
    };
    if (app.globalData.userInfo) check();
    else app.userReadyCallback = check;
  },

  onTitle(e) {
    this.setData({ title: e.detail.value });
  },
  onPlace(e) {
    this.setData({ place: e.detail.value });
  },
  onContent(e) {
    this.setData({ content: e.detail.value });
  },
  onDate(e) {
    this.setData({ date: e.detail.value });
  },
  onTime(e) {
    this.setData({ time: e.detail.value });
  },

  submit() {
    if (!this.data.title.trim()) {
      wx.showToast({ title: "请填写活动名称", icon: "none" });
      return;
    }
    if (!this.data.date) {
      wx.showToast({ title: "请选择活动时间", icon: "none" });
      return;
    }
    this.setData({ submitting: true });
    wx.showLoading({ title: "发布中…", mask: true });

    const activityTime = new Date(`${this.data.date}T${this.data.time}:00`).getTime();
    wx.cloud
      .callFunction({
        name: "api",
        data: {
          action: "publishActivity",
          data: {
            title: this.data.title.trim(),
            place: this.data.place.trim() || "待定",
            content: this.data.content.trim(),
            activityTime,
          },
        },
      })
      .then((res) => {
        wx.hideLoading();
        if (res.result.ok) {
          wx.showToast({ title: "发布成功" });
          setTimeout(() => wx.navigateBack(), 800);
        } else {
          wx.showToast({ title: res.result.msg, icon: "none" });
        }
      })
      .catch(() => {
        wx.hideLoading();
        wx.showToast({ title: "发布失败", icon: "none" });
      })
      .finally(() => this.setData({ submitting: false }));
  },
});
