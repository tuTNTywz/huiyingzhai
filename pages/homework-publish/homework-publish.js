const homework = require('../../utils/homework')
const user = require('../../utils/user')

const COURSES = ['高等数学', '大学英语', '数据结构', '线性代数', '大学物理', '程序设计']
const MAX_DESC = 200

function pad(num) {
  return num < 10 ? `0${num}` : `${num}`
}

function formatDate(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function formatSize(size) {
  if (!size) {
    return ''
  }
  if (size < 1024 * 1024) {
    return `${Math.max(1, Math.round(size / 1024))} KB`
  }
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

Page({
  data: {
    courses: COURSES,
    title: '',
    course: '',
    courseIndex: 0,
    teacher: '',
    date: '',
    time: '23:59',
    minDate: '',
    desc: '',
    descLength: 0,
    maxDesc: MAX_DESC,
    attachment: null,
    canSubmit: false,
    submitting: false
  },

  onLoad() {
    if (!user.isCourseRep()) {
      wx.showToast({ title: '仅课代表可上传作业', icon: 'none' })
      setTimeout(() => {
        wx.navigateBack()
      }, 800)
      return
    }

    const today = new Date()
    this.setData({
      date: formatDate(today),
      minDate: formatDate(today)
    })
  },

  onUnload() {
    if (this.timer) {
      clearTimeout(this.timer)
    }
  },

  onTitleInput(e) {
    const title = e.detail.value
    this.setData({
      title,
      canSubmit: !!title.trim()
    })
  },

  onCourseChange(e) {
    const index = Number(e.detail.value)
    this.setData({
      courseIndex: index,
      course: COURSES[index]
    })
  },

  onTeacherInput(e) {
    this.setData({ teacher: e.detail.value })
  },

  onDateChange(e) {
    this.setData({ date: e.detail.value })
  },

  onTimeChange(e) {
    this.setData({ time: e.detail.value })
  },

  onDescInput(e) {
    const desc = e.detail.value
    this.setData({
      desc,
      descLength: desc.length
    })
  },

  onChooseFile() {
    wx.chooseMessageFile({
      count: 1,
      type: 'file',
      success: (res) => {
        const file = res.tempFiles && res.tempFiles[0]
        if (!file) {
          return
        }
        this.setData({
          attachment: {
            name: file.name,
            path: file.path,
            sizeText: formatSize(file.size)
          }
        })
      },
      fail: (err) => {
        if (err && err.errMsg && err.errMsg.indexOf('cancel') > -1) {
          return
        }
        wx.showToast({ title: '选择文件失败', icon: 'none' })
      }
    })
  },

  onRemoveFile() {
    this.setData({ attachment: null })
  },

  onSubmit() {
    if (this.data.submitting) {
      return
    }
    const { title, course, teacher, date, time, desc, attachment } = this.data

    if (!title.trim()) {
      wx.showToast({ title: '请填写作业标题', icon: 'none' })
      return
    }
    if (!course) {
      wx.showToast({ title: '请选择所属课程', icon: 'none' })
      return
    }

    const due = new Date(`${date.replace(/-/g, '/')} ${time}:00`).getTime()
    if (!due || due <= Date.now()) {
      wx.showToast({ title: '截止时间需晚于当前时间', icon: 'none' })
      return
    }

    this.setData({ submitting: true })
    homework.publishAssignment({
      title: title.trim(),
      course,
      teacher: teacher.trim() || '课代表',
      desc: desc.trim(),
      attachment: attachment ? attachment.name : '',
      due
    })

    wx.showToast({ title: '发布成功', icon: 'success', duration: 800 })
    this.timer = setTimeout(() => {
      wx.navigateBack()
    }, 700)
  }
})
