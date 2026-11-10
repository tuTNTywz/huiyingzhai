// 通知中心：新作业通知列表 + 微信订阅消息提醒开关
const { requireRegistered } = require("../../utils/guard");
const {
  HOMEWORK_TMPL_ID,
  fetchNotices,
  markAllRead,
  isPushEnabled,
  requestHomeworkPush,
  disableHomeworkPush,
} = require("../../utils/notice");

Page({
  data: {
    list: [],
    loading: true,
    pushEnabled: false,
    pushSupported: !!HOMEWORK_TMPL_ID,
    subscribing: false,
  },

  onShow() {
    if (!requireRegistered()) return;
    this.setData({ pushEnabled: isPushEnabled() });
    this.load();
  },

  onPullDownRefresh() {
    this.load().finally(() => wx.stopPullDownRefresh());
  },

  load() {
    return fetchNotices()
      .then(({ list }) => {
        // 先渲染未读标记，再写入已读时间；下次进入不再显示未读
        this.setData({ list, loading: false });
        markAllRead(list);
      })
      .catch(() => {
        this.setData({ loading: false });
        wx.showToast({ title: "通知加载失败", icon: "none" });
      });
  },

  // 学科任务跳对应板块，班级作业跳作业详情
  openNotice(e) {
    const id = e.currentTarget.dataset.id;
    if (!id) return;
    const notice = this.data.list.find((n) => n._id && (n.homeworkId === id || n.subject === id));
    if (notice && notice.type === "subject_task") {
      wx.navigateTo({ url: `/pages/subject/index?key=${id}` });
    } else {
      wx.navigateTo({ url: `/pages/homework/detail?id=${id}` });
    }
  },

  // 提醒开关：开启时请求授权并登记一次推送额度；关闭时云端清零额度
  onPushToggle(e) {
    if (this.data.subscribing || !this.data.pushSupported) {
      this.setData({ pushEnabled: false });
      return;
    }
    const on = e.detail.value;
    this.setData({ subscribing: true, pushEnabled: on });
    const task = on ? requestHomeworkPush() : disableHomeworkPush();
    task
      .then(() =>
        wx.showToast({ title: on ? "已开启，新作业将推送提醒" : "已关闭提醒" })
      )
      .catch((msg) => {
        // 授权被拒或关闭失败：开关回退到关闭状态
        this.setData({ pushEnabled: false });
        wx.showToast({
          title: typeof msg === "string" ? msg : (msg && msg.errMsg) || "操作失败",
          icon: "none",
        });
      })
      .finally(() => this.setData({ subscribing: false }));
  },
});
