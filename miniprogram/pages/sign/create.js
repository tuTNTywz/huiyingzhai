// 发起签到（班委）
Page({
  data: {
    title: "",
    date: "",
    time: "23:59",
    minDate: "",
    submitting: false,
  },

  onLoad() {
    const now = new Date();
    const p = (n) => (n < 10 ? "0" + n : "" + n);
    this.setData({
      minDate: `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`,
    });
  },

  onTitle(e) {
    this.setData({ title: e.detail.value });
  },
  onDate(e) {
    this.setData({ date: e.detail.value });
  },
  onTime(e) {
    this.setData({ time: e.detail.value });
  },

  submit() {
    if (!this.data.title.trim()) {
      wx.showToast({ title: "请填写签到主题", icon: "none" });
      return;
    }
    if (!this.data.date) {
      wx.showToast({ title: "请选择截止时间", icon: "none" });
      return;
    }
    this.setData({ submitting: true });
    wx.showLoading({ title: "发起中…", mask: true });

    const endTime = new Date(`${this.data.date}T${this.data.time}:00`).getTime();
    wx.cloud
      .callFunction({
        name: "api",
        data: {
          action: "createSignTask",
          data: {
            title: this.data.title.trim(),
            startTime: Date.now(),
            endTime,
          },
        },
      })
      .then((res) => {
        wx.hideLoading();
        if (res.result.ok) {
          wx.showToast({ title: "已发起" });
          setTimeout(() => wx.redirectTo({ url: `/pages/sign/detail?id=${res.result.id}` }), 600);
        } else {
          wx.showToast({ title: res.result.msg, icon: "none" });
        }
      })
      .catch(() => {
        wx.hideLoading();
        wx.showToast({ title: "发起失败", icon: "none" });
      })
      .finally(() => this.setData({ submitting: false }));
  },
});
