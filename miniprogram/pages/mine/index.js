// 我的：个人信息 + 页内登记/修改资料弹窗
const app = getApp();
const { requireRegistered } = require("../../utils/guard");
const { POSITIONS, validateProfile, saveProfile } = require("../../utils/profile");
const { subjectName } = require("../../utils/subjects");

const emptyForm = () => ({ name: "", studentId: "", className: "", position: "" });

Page({
  data: {
    user: null,
    isCommittee: false,
    initial: "同",
    registered: false,
    subjectDisplayName: "—",
    showModal: false,
    positions: POSITIONS,
    positionIndex: 0,
    form: emptyForm(),
    submitting: false,
    submitText: "保 存",
  },

  onShow() {
    requireRegistered();
    const ready = () => {
      this.refresh();
      // 首次登录自动打开注册窗口
      if (app.globalData.needRegisterModal && !app.globalData.registered) {
        this.openRegister();
      }
    };
    if (app.globalData.userInfo) ready();
    else app.userReadyCallback = ready;
    // 班长指定课代表后，刷新最新资料用于个人信息展示
    app.refreshUser().then(() => this.refresh());
  },

  refresh() {
    const u = app.globalData.userInfo;
    if (!u) return;
    this.setData({
      user: u,
      isCommittee: u.role === "committee",
      registered: !!u.registered,
      subjectDisplayName: subjectName(u.subject) || "—",
      submitText: u.registered ? "保 存" : "登 记 入 斋",
      initial: (u.name || u.nickname || "同").charAt(0),
    });
  },

  // 打开注册窗口，已登记时预填个人信息
  openRegister() {
    const u = app.globalData.userInfo || {};
    const posIdx = Math.max(0, POSITIONS.indexOf(u.position));
    this.setData({
      showModal: true,
      positionIndex: posIdx,
      form: {
        name: u.name || "",
        studentId: u.studentId || "",
        className: u.className || "",
        position: u.position || "",
      },
    });
  },

  closeRegister() {
    // 首次登记未完成前不可关闭窗口
    if (!this.data.registered) {
      wx.showToast({ title: "请先完成登记", icon: "none" });
      return;
    }
    this.setData({ showModal: false });
  },

  noop() {},

  onField(e) {
    const field = e.currentTarget.dataset.field;
    this.setData({ [`form.${field}`]: e.detail.value });
  },

  onPosition(e) {
    const idx = Number(e.detail.value);
    this.setData({ positionIndex: idx, "form.position": POSITIONS[idx] });
  },

  submit() {
    if (this.data.submitting) return;
    const err = validateProfile(this.data.form);
    if (err) {
      wx.showToast({ title: err, icon: "none" });
      return;
    }
    const wasRegistered = this.data.registered;
    this.setData({ submitting: true });
    wx.showLoading({ title: "提交中…", mask: true });

    saveProfile(this.data.form)
      .then(() => {
        wx.hideLoading();
        wx.showToast({ title: wasRegistered ? "资料已更新" : "登记完成" });
        this.setData({ showModal: false });
        this.refresh();
      })
      .catch((msg) => {
        wx.hideLoading();
        wx.showToast({ title: typeof msg === "string" ? msg : "网络异常，请重试", icon: "none" });
      })
      .finally(() => this.setData({ submitting: false }));
  },
});
