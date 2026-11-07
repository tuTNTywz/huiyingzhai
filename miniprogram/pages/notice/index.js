// 通知中心：新作业通知列表 + 微信订阅消息提醒开关
const { requireRegistered } = require("../../utils/guard");
const {
  HOMEWORK_TMPL_ID,
  fetchNotices,
  markAllRead,
  requestHomeworkPush,
} = require("../../utils/notice");

Page({
  data: {
    list: [],
    loading: true,
    pushEnabled: !!HOMEWORK_TMPL_ID,
    subscribing: false,
  },

  onShow() {
    if (!requireRegistered()) return;
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

  openNotice(e) {
    const id = e.currentTarget.dataset.id;
    if (id) wx.navigateTo({ url: `/pages/homework/detail?id=${id}` });
  },

  enablePush() {
    if (this.data.subscribing) return;
    this.setData({ subscribing: true });
    requestHomeworkPush()
      .then(() => wx.showToast({ title: "已开启，新作业将推送提醒" }))
      .catch((msg) => {
        wx.showToast({
          title: typeof msg === "string" ? msg : "开启失败",
          icon: "none",
        });
      })
      .finally(() => this.setData({ subscribing: false }));
  },
});
