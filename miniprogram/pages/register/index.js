// 入斋登记：首次登录注册（姓名、学号、班级、职位），亦支持修改资料
const app = getApp();
const { POSITIONS, validateProfile, saveProfile } = require("../../utils/profile");

Page({
  data: {
    editMode: false,
    positions: POSITIONS,
    name: "",
    studentId: "",
    className: "",
    position: "",
    positionIndex: 0,
    submitting: false,
  },

  onLoad(options) {
    const editMode = options.edit === "1";
    this.setData({ editMode });
    if (editMode) {
      wx.setNavigationBarTitle({ title: "修改资料" });
      const u = app.globalData.userInfo || {};
      const idx = Math.max(0, POSITIONS.indexOf(u.position));
      this.setData({
        name: u.name || "",
        studentId: u.studentId || "",
        className: u.className || "",
        position: u.position || "",
        positionIndex: idx,
      });
    }
  },

  onName(e) {
    this.setData({ name: e.detail.value });
  },
  onStudentId(e) {
    this.setData({ studentId: e.detail.value });
  },
  onClassName(e) {
    this.setData({ className: e.detail.value });
  },
  onPosition(e) {
    const idx = Number(e.detail.value);
    this.setData({ positionIndex: idx, position: POSITIONS[idx] });
  },

  submit() {
    const form = {
      name: this.data.name,
      studentId: this.data.studentId,
      className: this.data.className,
      position: this.data.position,
    };
    const err = validateProfile(form);
    if (err) {
      wx.showToast({ title: err, icon: "none" });
      return;
    }
    this.setData({ submitting: true });
    wx.showLoading({ title: this.data.editMode ? "保存中…" : "登记中…", mask: true });

    saveProfile(form)
      .then(() => {
        wx.hideLoading();
        wx.showToast({ title: this.data.editMode ? "已保存" : "登记完成" });
        setTimeout(() => {
          if (this.data.editMode) wx.navigateBack();
          else wx.switchTab({ url: "/pages/homework/index" });
        }, 700);
      })
      .catch((msg) => {
        wx.hideLoading();
        wx.showToast({ title: typeof msg === "string" ? msg : "网络异常，请重试", icon: "none" });
      })
      .finally(() => this.setData({ submitting: false }));
  },
});
