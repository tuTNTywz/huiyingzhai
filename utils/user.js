const ROLE_KEY = 'user_role_v1'

/**
 * 当前用户角色：rep（课代表）/ student（普通同学）
 * 演示环境默认课代表，接入登录后可改为读取后台返回的角色
 */
function getRole() {
  try {
    return wx.getStorageSync(ROLE_KEY) || 'rep'
  } catch (e) {
    return 'rep'
  }
}

function setRole(role) {
  wx.setStorageSync(ROLE_KEY, role)
}

function isCourseRep() {
  return getRole() === 'rep'
}

module.exports = {
  getRole,
  setRole,
  isCourseRep
}
