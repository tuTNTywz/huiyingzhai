const homework = require('../../utils/homework')
const user = require('../../utils/user')

Page({
  data: {
    isRep: false,
    tabs: [
      { key: 'all', label: '全部', count: 0 },
      { key: 'pending', label: '待完成', count: 0 },
      { key: 'done', label: '已完成', count: 0 }
    ],
    activeTab: 'all',
    list: [],
    stats: {
      total: 0,
      done: 0,
      pending: 0,
      overdue: 0,
      percent: 0
    }
  },

  onShow() {
    this.setData({ isRep: user.isCourseRep() })
    this.refresh()
  },

  refresh() {
    const all = homework.getList()
    const stats = homework.getStats()
    const activeTab = this.data.activeTab
    const expandedMap = {}
    this.data.list.forEach((item) => {
      if (item.expanded) {
        expandedMap[item.id] = true
      }
    })

    const list = all
      .filter((item) => {
        if (activeTab === 'pending') {
          return !item.done
        }
        if (activeTab === 'done') {
          return item.done
        }
        return true
      })
      .map((item) => ({ ...item, expanded: !!expandedMap[item.id] }))

    this.setData({
      list,
      stats,
      tabs: [
        { key: 'all', label: '全部', count: stats.total },
        { key: 'pending', label: '待完成', count: stats.pending },
        { key: 'done', label: '已完成', count: stats.done }
      ]
    })
  },

  onTabChange(e) {
    const { key } = e.currentTarget.dataset
    if (key === this.data.activeTab) {
      return
    }
    this.setData({ activeTab: key, list: [] }, () => {
      this.refresh()
    })
  },

  onToggleDetail(e) {
    const id = e.currentTarget.dataset.id
    const list = this.data.list.map((item) => {
      if (item.id === id) {
        return { ...item, expanded: !item.expanded }
      }
      return item
    })
    this.setData({ list })
  },

  onSubmit(e) {
    const id = e.currentTarget.dataset.id
    const target = this.data.list.find((item) => item.id === id)
    if (!target) {
      return
    }

    wx.showModal({
      title: '提交作业',
      content: `确认提交《${target.title}》？提交后可在已完成列表中撤销。`,
      confirmText: '确认提交',
      confirmColor: '#3B6EF6',
      success: (res) => {
        if (!res.confirm) {
          return
        }
        homework.setDone(id, true)
        this.refresh()
        wx.showToast({ title: '提交成功', icon: 'success' })
      }
    })
  },

  onUndo(e) {
    const id = e.currentTarget.dataset.id
    homework.setDone(id, false)
    this.refresh()
    wx.showToast({ title: '已撤销提交', icon: 'none' })
  },

  onPublish() {
    if (!user.isCourseRep()) {
      wx.showToast({ title: '仅课代表可上传作业', icon: 'none' })
      return
    }
    wx.navigateTo({ url: '/pages/homework-publish/homework-publish' })
  },

  onWithdraw(e) {
    const id = e.currentTarget.dataset.id
    const target = this.data.list.find((item) => item.id === id)
    if (!target) {
      return
    }

    wx.showModal({
      title: '撤回作业',
      content: `确认撤回《${target.title}》？撤回后同学将不再看到这份作业。`,
      confirmText: '撤回',
      confirmColor: '#E0526A',
      success: (res) => {
        if (!res.confirm) {
          return
        }
        homework.removePublished(id)
        this.refresh()
        wx.showToast({ title: '已撤回', icon: 'none' })
      }
    })
  },

  onShareAppMessage() {
    return {
      title: '作业列表',
      path: '/pages/homework/homework'
    }
  }
})
