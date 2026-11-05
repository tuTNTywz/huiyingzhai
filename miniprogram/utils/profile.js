// 学籍登记共用逻辑：登记页与「我的」弹窗复用
const POSITIONS = [
  "普通同学", "班长", "副班长", "团支书", "学习委员", "生活委员",
  "文艺委员", "体育委员", "组织委员", "宣传委员", "纪律委员",
];

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

module.exports = { POSITIONS, validateProfile, saveProfile };
