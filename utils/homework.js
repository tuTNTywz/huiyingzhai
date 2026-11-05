const STORAGE_KEY = 'homework_state_v1'
const PUBLISHED_KEY = 'homework_published_v1'
const HOUR = 60 * 60 * 1000
const NOW = Date.now()

const ASSIGNMENTS = [
  {
    id: 'hw-1',
    title: '高数第三章课后习题',
    course: '高等数学',
    teacher: '李老师',
    desc: '完成教材 P78 第 1—12 题，需写出完整解题步骤，拍照后合并为一个 PDF 上传。',
    attachment: '第三章习题.pdf',
    due: NOW + 6 * HOUR,
    defaultDone: false
  },
  {
    id: 'hw-2',
    title: '大学英语 Unit 3 作文',
    course: '大学英语',
    teacher: '王老师',
    desc: '以 “My Campus Life” 为题写一篇不少于 150 词的短文，注意时态与段落结构。',
    attachment: '写作要求.docx',
    due: NOW + 30 * HOUR,
    defaultDone: false
  },
  {
    id: 'hw-3',
    title: '数据结构实验报告一',
    course: '数据结构',
    teacher: '张老师',
    desc: '实现顺序表的基本操作，提交源代码与实验报告，报告需包含测试用例截图与复杂度分析。',
    attachment: '实验指导书.pdf',
    due: NOW - 12 * HOUR,
    defaultDone: false
  },
  {
    id: 'hw-4',
    title: '线性代数第 2 章练习',
    course: '线性代数',
    teacher: '赵老师',
    desc: '完成课后习题 2.1—2.5，矩阵运算过程需要手写拍照上传。',
    attachment: '',
    due: NOW + 72 * HOUR,
    defaultDone: true
  }
]

function pad(num) {
  return num < 10 ? `0${num}` : `${num}`
}

function getOverrides() {
  try {
    return wx.getStorageSync(STORAGE_KEY) || {}
  } catch (e) {
    return {}
  }
}

function setDone(id, done) {
  const overrides = getOverrides()
  overrides[id] = done
  wx.setStorageSync(STORAGE_KEY, overrides)
}

function getPublished() {
  try {
    const list = wx.getStorageSync(PUBLISHED_KEY)
    return Array.isArray(list) ? list : []
  } catch (e) {
    return []
  }
}

function savePublished(list) {
  wx.setStorageSync(PUBLISHED_KEY, list)
}

function publishAssignment(payload) {
  const list = getPublished()
  const item = {
    id: `hw-p-${Date.now()}`,
    title: payload.title,
    course: payload.course,
    teacher: payload.teacher,
    desc: payload.desc,
    attachment: payload.attachment,
    due: payload.due,
    defaultDone: false,
    published: true,
    createdAt: Date.now()
  }
  list.unshift(item)
  savePublished(list)
  return item
}

function removePublished(id) {
  const list = getPublished().filter((item) => item.id !== id)
  savePublished(list)
}

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
}

function formatDue(ts) {
  const due = new Date(ts)
  const dayDiff = Math.round((startOfDay(due) - startOfDay(new Date())) / (24 * HOUR))
  const time = `${pad(due.getHours())}:${pad(due.getMinutes())}`

  if (dayDiff === 0) {
    return `今天 ${time}`
  }
  if (dayDiff === 1) {
    return `明天 ${time}`
  }
  if (dayDiff === -1) {
    return `昨天 ${time}`
  }
  return `${due.getMonth() + 1}月${due.getDate()}日 ${time}`
}

function getList() {
  const overrides = getOverrides()
  const now = Date.now()
  const source = ASSIGNMENTS.concat(getPublished())

  return source.map((item) => {
    const done = Object.prototype.hasOwnProperty.call(overrides, item.id)
      ? overrides[item.id]
      : item.defaultDone
    const overdue = !done && now > item.due
    let statusKey = 'pending'
    let statusText = '待完成'

    if (done) {
      statusKey = 'done'
      statusText = '已提交'
    } else if (overdue) {
      statusKey = 'overdue'
      statusText = '已逾期'
    } else if (item.due - now <= 24 * HOUR) {
      statusKey = 'urgent'
      statusText = '即将截止'
    }

    return {
      id: item.id,
      title: item.title,
      course: item.course,
      teacher: item.teacher,
      desc: item.desc,
      attachment: item.attachment,
      due: item.due,
      dueText: formatDue(item.due),
      done,
      statusKey,
      statusText,
      published: !!item.published,
      expanded: false
    }
  }).sort((a, b) => {
    if (a.done !== b.done) {
      return a.done ? 1 : -1
    }
    return a.due - b.due
  })
}

function getStats() {
  const list = getList()
  const total = list.length
  const done = list.filter((item) => item.done).length
  const overdue = list.filter((item) => item.statusKey === 'overdue').length

  return {
    total,
    done,
    overdue,
    pending: total - done,
    percent: total ? Math.round((done / total) * 100) : 0
  }
}

module.exports = {
  getList,
  getStats,
  setDone,
  getPublished,
  publishAssignment,
  removePublished
}
