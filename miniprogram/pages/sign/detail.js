// 签到详情：同学在线签到，班委查看签到记录
const app = getApp();
const { formatTime, toDateMs } = require("../../utils/util");

Page({
  data: {
    task: null,
    records: [],
    myRecord: null,
    status: "", // active / ended
    isCommittee: false,
    signing: false,
    countText: "",
  },

  onLoad(options) {
    this.id = options.id;
  },

  onShow() {
    const refresh = () => this.setData({ isCommittee: app.isCommittee() });
    if (app.globalData.userInfo) refresh();
    else app.userReadyCallback = refresh;
    this.fetchAll();
  },

  onPullDownRefresh() {
    this.fetchAll();
    wx.stopPullDownRefresh();
  },

  fetchAll() {
    const db = wx.cloud.database();
    db.collection("sign_tasks")
      .doc(this.id)
      .get()
      .then((res) => {
        const t = res.data;
        const end = toDateMs(t.endTime) || t.endTime;
        const status = Date.now() <= end ? "active" : "ended";
        this.setData({
          task: { ...t, endText: formatTime(end) },
          status,
        });
        // 查询签到记录（所有用户可读）
        return db
          .collection("sign_records")
          .where({ taskId: this.id })
          .orderBy("createTime", "asc")
          .limit(100)
          .get()
          .then((r) => {
            const me = app.globalData.userInfo;
            const records = r.data.map((x) => ({
              ...x,
              timeText: x.signTime && x.signTime.$date ? formatTime(x.signTime.$date) : "",
            }));
            const myRecord = records.find((x) => x._openid === me?._openid) || null;
            this.setData({
              records,
              myRecord,
              countText: `共 ${records.length} 人签到`,
            });
          });
      })
      .catch(() => wx.showToast({ title: "签到任务不存在", icon: "none" }));
  },

  // 同学在线签到
  doSign() {
    if (this.data.signing) return;
    this.setData({ signing: true });
    wx.cloud
      .callFunction({
        name: "api",
        data: { action: "signIn", data: { taskId: this.id } },
      })
      .then((res) => {
        if (res.result.ok) {
          wx.showToast({ title: "签到成功", icon: "success" });
          this.fetchAll();
        } else {
          wx.showModal({ title: "签到失败", content: res.result.msg, showCancel: false });
        }
      })
      .catch(() => wx.showToast({ title: "网络异常", icon: "none" }))
      .finally(() => this.setData({ signing: false }));
  },
});
