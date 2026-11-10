// 我的：个人信息 + 页内登记/修改资料弹窗
const app = getApp();
const { requireRegistered } = require("../../utils/guard");
const { validateProfile, saveProfile, matchPosition, matchRepSubjects, DEFAULT_CLASS } = require("../../utils/profile");
const { subjectName } = require("../../utils/subjects");

const emptyForm = () => ({ name: "", studentId: "", className: DEFAULT_CLASS, position: "" });

Page({
  data: {
    user: null,
    isCommittee: false,
    isAdmin: false,
    initial: "同",
    registered: false,
    subjectDisplayName: "—",
    showModal: false,
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
    // 管理员指定课代表后，刷新最新资料用于个人信息展示
    app.refreshUser().then(() => this.refresh());
  },

  refresh() {
    const u = app.globalData.userInfo;
    if (!u) return;
    this.setData({
      user: u,
      isCommittee: u.role === "committee" || u.role === "admin",
      isAdmin: u.role === "admin",
      registered: !!u.registered,
      submitText: u.registered ? "保 存" : "登 记 入 斋",
      initial: (u.name || u.nickname || "同").charAt(0),
    });
    // 负责科目从名单 roster 集合的 repSubjects 字段读取（按学号查询）
    matchRepSubjects(u.studentId).then((keys) => {
      const names = keys
        .map((k) => subjectName(k))
        .filter(Boolean);
      this.setData({ subjectDisplayName: names.join("、") || "—" });
    });
  },

  // 打开注册窗口，已登记时预填个人信息（职务按名单自动匹配）
  openRegister() {
    const u = app.globalData.userInfo || {};
    this.setData({
      showModal: true,
      form: {
        name: u.name || "",
        studentId: u.studentId || "",
        className: DEFAULT_CLASS,
        position: "",
      },
    });
    // 职务由数据库名单根据学号自动匹配
    matchPosition(u.studentId).then((p) => this.setData({ "form.position": p }));
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
    const patch = { [`form.${field}`]: e.detail.value };
    this.setData(patch);
    // 职务由数据库名单根据学号自动匹配
    if (field === "studentId") {
      matchPosition(e.detail.value).then((p) => this.setData({ "form.position": p }));
    }
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
