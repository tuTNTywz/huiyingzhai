// 发布志愿活动（宣传委员）
const api = require("../../utils/api");

Page({
  data: {
    title: "",
    content: "",
    place: "",
    activityDate: "",
    activityStart: "09:00",
    activityEnd: "11:00",
    signupMethod: "小程序内报名",
    methodOptions: ["小程序内报名", "群内接龙", "现场登记", "联系负责人"],
    date: "", // 报名截止日期
    time: "23:59", // 报名截止时间
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
  onContent(e) {
    this.setData({ content: e.detail.value });
  },
  onPlace(e) {
    this.setData({ place: e.detail.value });
  },
  onActivityDate(e) {
    this.setData({ activityDate: e.detail.value });
  },
  onActivityStart(e) {
    this.setData({ activityStart: e.detail.value });
  },
  onActivityEnd(e) {
    this.setData({ activityEnd: e.detail.value });
  },
  onSignupMethod(e) {
    this.setData({ signupMethod: e.detail.value });
  },
  onMethodChip(e) {
    this.setData({ signupMethod: e.currentTarget.dataset.value });
  },
  onDate(e) {
    this.setData({ date: e.detail.value });
  },
  onTime(e) {
    this.setData({ time: e.detail.value });
  },

  submit() {
    if (!this.data.title.trim()) {
      wx.showToast({ title: "请填写志愿活动主题", icon: "none" });
      return;
    }
    if (!this.data.content.trim()) {
      wx.showToast({ title: "请填写志愿活动内容", icon: "none" });
      return;
    }
    if (!this.data.place.trim()) {
      wx.showToast({ title: "请填写志愿活动地点", icon: "none" });
      return;
    }
    if (!this.data.activityDate) {
      wx.showToast({ title: "请选择志愿活动日期", icon: "none" });
      return;
    }
    if (this.data.activityEnd <= this.data.activityStart) {
      wx.showToast({ title: "结束时间需晚于开始时间", icon: "none" });
      return;
    }
    if (!this.data.signupMethod.trim()) {
      wx.showToast({ title: "请填写报名方式", icon: "none" });
      return;
    }
    if (!this.data.date) {
      wx.showToast({ title: "请选择报名截止时间", icon: "none" });
      return;
    }

    this.setData({ submitting: true });
    wx.showLoading({ title: "发布中…", mask: true });

    const activityStartTime = new Date(
      `${this.data.activityDate}T${this.data.activityStart}:00`
    ).getTime();
    const activityEndTime = new Date(
      `${this.data.activityDate}T${this.data.activityEnd}:00`
    ).getTime();
    const endTime = new Date(`${this.data.date}T${this.data.time}:00`).getTime();

    api
      .call("createVolunteer", {
        title: this.data.title.trim(),
        content: this.data.content.trim(),
        place: this.data.place.trim(),
        activityStartTime,
        activityEndTime,
        signupMethod: this.data.signupMethod.trim(),
        startTime: Date.now(),
        endTime,
      })
      .then((res) => {
        wx.hideLoading();
        if (res.ok) {
          wx.showToast({ title: "已发布" });
          setTimeout(() => wx.redirectTo({ url: `/pages/sign/detail?id=${res.id}` }), 600);
        } else {
          wx.showToast({ title: res.msg, icon: "none" });
        }
      })
      .catch(() => {
        wx.hideLoading();
        wx.showToast({ title: "发布失败", icon: "none" });
      })
      .finally(() => this.setData({ submitting: false }));
  },
});
