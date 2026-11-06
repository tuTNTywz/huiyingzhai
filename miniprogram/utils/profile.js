// 学籍登记共用逻辑：登记页与「我的」弹窗复用
// 职位与角色均由名单字典（roster.js）根据学号自动确定
const { POSITIONS, getPositionFor } = require("./roster");

// 班级固定为本班，无需手动填写
const DEFAULT_CLASS = "人工智能26A3班";

function validateProfile(form = {}) {
  if (!String(form.name || "").trim()) return "请填写姓名";
  if (!String(form.studentId || "").trim()) return "请填写学号";
  if (!String(form.className || "").trim()) return "请填写班级";
  if (!form.position) return "请选择职位";
  return "";
}

function saveProfile(form) {
  const app = getApp();
  return wx.cloud
    .callFunction({
      name: "api",
      data: {
        action: "register",
        data: {
          name: String(form.name).trim(),
          studentId: String(form.studentId).trim(),
          className: String(form.className).trim(),
          position: form.position,
          // 课代表由管理员在课程设置中统一指定，登记时不可自选
        },
      },
    })
    .then((res) => {
      const r = res.result || {};
      if (!r.ok) return Promise.reject(r.msg || "登记失败");
      app.updateUser(r.user);
      return r.user;
    });
}

module.exports = { POSITIONS, getPositionFor, DEFAULT_CLASS, validateProfile, saveProfile };
