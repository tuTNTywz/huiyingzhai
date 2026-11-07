// 学科板块页：课程设置（管理员编辑） + 课代表发布任务通知
// 课代表由名单 roster 集合的 repSubjects 字段指定
const app = getApp();
const { formatTime, toDateMs } = require("../../utils/util");
const { requireRegistered } = require("../../utils/guard");
const { getSubject } = require("../../utils/subjects");
const { isSubjectRep } = require("../../utils/profile");

const emptyTaskForm = () => ({ title: "", content: "", date: "", time: "23:59" });
const emptySettingsForm = () => ({ teacher: "", credit: "", schedule: "" });

Page({
  data: {
    key: "",
    subjectName: "",
    ordinal: "",
    fallbackRep: "暂未设置",
    settings: {},
    hasSettings: false,
    creditText: "待设置",
    isMonitor: false,
    canManage: false,
    list: [],
    loading: true,
    expandedId: "",
    showPublish: false,
    form: emptyTaskForm(),
    minDate: "",
    submitting: false,
    showSettings: false,
    settingsForm: emptySettingsForm(),
    savingSettings: false,
  },

  onLoad(options) {
    const subject = getSubject(options.key);
    if (!subject) {
      wx.showToast({ title: "板块不存在", icon: "none" });
      setTimeout(() => wx.navigateBack(), 600);
      return;
    }
    wx.setNavigationBarTitle({ title: subject.name });
    const now = new Date();
    const p = (n) => (n < 10 ? "0" + n : "" + n);
    this.setData({
      key: subject.key,
      subjectName: subject.name,
      ordinal: subject.ordinal,
      minDate: `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`,
    });
  },

  onShow() {
    if (!this.data.key) return;
    requireRegistered();
    const ready = () => {
      const u = app.globalData.userInfo || {};
      this.setData({ isMonitor: u.role === "admin" });
      this.updateCanManage();
    };
    if (app.globalData.userInfo) ready();
    else app.userReadyCallback = ready;
    this.fetchSettings();
    this.fetchRep();
    this.fetchList();
  },

  onPullDownRefresh() {
    this.fetchSettings();
    this.fetchRep();
    this.fetchList();
    wx.stopPullDownRefresh();
  },

  // 名单指定的课代表（或班委/管理员）可发布任务通知
  updateCanManage() {
    const u = app.globalData.userInfo || {};
    isSubjectRep(this.data.key).then((isRep) => {
      this.setData({ canManage: app.isCommittee() || isRep });
    });
  },

  fetchSettings() {
    wx.cloud
      .database()
      .collection("subject_settings")
      .where({ subject: this.data.key })
      .limit(1)
      .get()
      .then((res) => {
        const s = res.data[0] || null;
        this.setData({
          settings: s || {},
          hasSettings: !!s,
          creditText: s && s.credit ? `${s.credit} 学分` : "待设置",
        });
        this.updateCanManage();
      })
      .catch(() => {});
  },

  // 从名单 roster 集合查询该科目课代表姓名
  fetchRep() {
    wx.cloud
      .database()
      .collection("roster")
      .where({ repSubjects: this.data.key })
      .limit(1)
      .get()
      .then((res) => {
        const rep = res.data[0];
        this.setData({ fallbackRep: rep ? rep.name || rep.studentId : "暂未设置" });
      })
      .catch(() => {});
  },

  fetchList() {
    wx.showLoading({ title: "载入中…" });
    wx.cloud
      .database()
      .collection("subject_tasks")
      .where({ subject: this.data.key })
      .orderBy("createTime", "desc")
      .limit(50)
      .get()
      .then((res) => {
        const now = Date.now();
        const list = res.data.map((t) => {
          const deadlineMs = toDateMs(t.deadline) || t.deadline || 0;
          return {
            ...t,
            deadlineText: deadlineMs ? formatTime(deadlineMs) : "",
            expired: deadlineMs ? deadlineMs < now : false,
            createTimeText: formatTime(toDateMs(t.createTime)),
          };
        });
        this.setData({ list, loading: false });
      })
      .catch(() => {
        this.setData({ loading: false });
        wx.showToast({ title: "加载失败", icon: "none" });
      })
      .finally(() => wx.hideLoading());
  },

  toggleExpand(e) {
    const id = e.currentTarget.dataset.id;
    this.setData({ expandedId: this.data.expandedId === id ? "" : id });
  },

  // ===== 课程设置（管理员） =====
  openSettings() {
    if (!this.data.isMonitor) return;
    const s = this.data.settings;
    this.setData({
      showSettings: true,
      settingsForm: {
        teacher: s.teacher || "",
        credit: s.credit || "",
        schedule: s.schedule || "",
      },
    });
  },

  closeSettings() {
    this.setData({ showSettings: false });
  },

  onSettingsField(e) {
    const field = e.currentTarget.dataset.field;
    this.setData({ [`settingsForm.${field}`]: e.detail.value });
  },

  saveSettings() {
    if (!this.data.isMonitor || this.data.savingSettings) return;
    this.setData({ savingSettings: true });
    wx.showLoading({ title: "保存中…", mask: true });

    wx.cloud
      .callFunction({
        name: "api",
        data: {
          action: "saveSubjectSettings",
          data: { subject: this.data.key, ...this.data.settingsForm },
        },
      })
      .then((res) => {
        wx.hideLoading();
        if (res.result.ok) {
          wx.showToast({ title: "设置已保存" });
          this.setData({ showSettings: false });
          this.fetchSettings();
          this.fetchRep();
          this.updateCanManage();
        } else {
          wx.showToast({ title: res.result.msg, icon: "none" });
        }
      })
      .catch(() => {
        wx.hideLoading();
        wx.showToast({ title: "保存失败", icon: "none" });
      })
      .finally(() => this.setData({ savingSettings: false }));
  },

  // ===== 任务通知 =====
  openPublish() {
    this.setData({ showPublish: true, form: emptyTaskForm() });
  },
  closePublish() {
    this.setData({ showPublish: false });
  },
  noop() {},

  onField(e) {
    const field = e.currentTarget.dataset.field;
    this.setData({ [`form.${field}`]: e.detail.value });
  },
  onDate(e) {
    this.setData({ "form.date": e.detail.value });
  },
  onTime(e) {
    this.setData({ "form.time": e.detail.value });
  },

  publish() {
    const form = this.data.form;
    if (!form.title.trim()) {
      wx.showToast({ title: "请填写通知标题", icon: "none" });
      return;
    }
    if (this.data.submitting) return;
    this.setData({ submitting: true });
    wx.showLoading({ title: "发布中…", mask: true });

    const deadline = form.date ? new Date(`${form.date}T${form.time}:00`).getTime() : 0;
    wx.cloud
      .callFunction({
        name: "api",
        data: {
          action: "publishSubjectTask",
          data: {
            subject: this.data.key,
            title: form.title.trim(),
            content: form.content.trim(),
            deadline,
          },
        },
      })
      .then((res) => {
        wx.hideLoading();
        if (res.result.ok) {
          wx.showToast({ title: "已发布" });
          this.setData({ showPublish: false });
          this.fetchList();
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

  onDelete(e) {
    if (!this.data.canManage) return;
    const id = e.currentTarget.dataset.id;
    wx.showModal({
      title: "删除通知",
      content: "确定删除这条任务通知吗？",
      confirmColor: "#b5543c",
      success: (res) => {
        if (!res.confirm) return;
        wx.cloud.callFunction({
          name: "api",
          data: { action: "deleteSubjectTask", data: { id } },
          success: (r) => {
            if (r.result.ok) {
              wx.showToast({ title: "已删除" });
              this.fetchList();
            } else {
              wx.showToast({ title: r.result.msg, icon: "none" });
            }
          },
        });
      },
    });
  },
});
