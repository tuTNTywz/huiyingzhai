const homework = require('../../utils/homework')

const WEEK = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六']

function pad(num) {
  return num < 10 ? `0${num}` : `${num}`
}

Page({
  data: {
    greeting: '你好',
    dateText: '',
    stats: [
      { label: '今日课程', value: '3' },
      { label: '待交作业', value: '2' },
      { label: '未读通知', value: '5' }
    ],
    quickEntries: [
      { id: 'schedule', name: '课程表', icon: '课', bg: '#EAF1FF', color: '#3B6EF6' },
      { id: 'homework', name: '作业', icon: '作', bg: '#FFF0E6', color: '#F58A2E' },
      { id: 'notice', name: '通知', icon: '告', bg: '#E9F8F1', color: '#22A06B' },
      { id: 'resource', name: '资料', icon: '资', bg: '#F1EDFF', color: '#6C5CE7' },
      { id: 'sign', name: '签到', icon: '签', bg: '#E8F6FB', color: '#1595B6' },
      { id: 'exam', name: '考试', icon: '考', bg: '#FFEDF0', color: '#E0526A' },
      { id: 'activity', name: '活动', icon: '活', bg: '#FFF7E0', color: '#D79A00' },
      { id: 'more', name: '更多', icon: '更', bg: '#F2F4F8', color: '#5A6474' }
    ],
    courses: [
      {
        id: 1,
        start: '08:30',
        end: '10:05',
        name: '高等数学',
        room: '教学楼 A302',
        teacher: '李老师',
        status: '进行中',
        live: true,
        dot: '#3B6EF6'
      },
      {
        id: 2,
        start: '10:25',
        end: '12:00',
        name: '大学英语',
        room: '教学楼 B201',
        teacher: '王老师',
        status: '待开始',
        live: false,
        dot: '#22A06B'
      },
      {
        id: 3,
        start: '14:00',
        end: '15:35',
        name: '数据结构',
        room: '实验楼 C105',
        teacher: '张老师',
        status: '待开始',
        live: false,
        dot: '#6C5CE7'
      }
    ],
    notices: [
      {
        id: 1,
        title: '关于本周五调课安排的通知',
        tag: '教务',
        time: '10分钟前',
        unread: true
      },
      {
        id: 2,
        title: '第一次小组作业提交要求',
        tag: '作业',
        time: '2小时前',
        unread: true
      },
      {
        id: 3,
        title: '班级秋游活动报名开始',
        tag: '活动',
        time: '昨天',
        unread: false
      }
    ]
  },

  onLoad() {
    const now = new Date()
    const hour = now.getHours()
    let greeting = '晚上好'
    if (hour < 6) {
      greeting = '夜深了'
    } else if (hour < 11) {
      greeting = '早上好'
    } else if (hour < 14) {
      greeting = '中午好'
    } else if (hour < 18) {
      greeting = '下午好'
    }

    this.setData({
      greeting,
      dateText: `${now.getMonth() + 1}月${now.getDate()}日 ${WEEK[now.getDay()]}`
    })
  },

  onShow() {
    const { pending } = homework.getStats()
    const stats = this.data.stats.map((item, index) => {
      if (index === 1 && item.value !== String(pending)) {
        return { ...item, value: String(pending) }
      }
      return item
    })
    this.setData({ stats })
  },

  onSearchTap() {
    wx.showToast({ title: '搜索功能开发中', icon: 'none' })
  },

  onQuickTap(e) {
    const { name } = e.currentTarget.dataset
    if (name === '作业') {
      wx.navigateTo({ url: '/pages/homework/homework' })
      return
    }
    wx.showToast({ title: `${name}功能开发中`, icon: 'none' })
  },

  onCourseTap(e) {
    const { name } = e.currentTarget.dataset
    wx.showToast({ title: `查看《${name}》详情`, icon: 'none' })
  },

  onNoticeTap(e) {
    const id = Number(e.currentTarget.dataset.id)
    const notices = this.data.notices.map((item) => {
      if (item.id === id) {
        return { ...item, unread: false }
      }
      return item
    })
    this.setData({ notices })
  },

  onStatTap(e) {
    const { label } = e.currentTarget.dataset
    if (label === '待交作业') {
      wx.navigateTo({ url: '/pages/homework/homework' })
      return
    }
    wx.showToast({ title: `查看${label}`, icon: 'none' })
  },

  onMoreTap(e) {
    const { target } = e.currentTarget.dataset
    wx.showToast({ title: `进入${target}`, icon: 'none' })
  },

  onShareAppMessage() {
    return {
      title: '班级助手',
      path: '/pages/index/index'
    }
  }
})
