// 学籍登记共用逻辑：登记页与「我的」弹窗复用
// 职务与角色以后端 roster 名单为准，自动根据学号确定
const api = require("./api");

// 班级固定为本班，无需手动填写
const DEFAULT_CLASS = "人工智能26A3班";

// 名单缓存：学号 → 记录（按学号向后端查询 roster）
const rosterCache = {}; // studentId -> 记录对象或 null（null 表示查询过但未命中）

function loadStudent(studentId) {
  const id = String(studentId || "").trim();
  if (!id) return Promise.resolve(null);
  if (id in rosterCache) return Promise.resolve(rosterCache[id]);
  return api
    .call("lookupRoster", { studentId: id })
    .then((res) => {
      rosterCache[id] = (res && res.ok && res.item) || null;
      return rosterCache[id];
    })
    .catch(() => (rosterCache[id] = null));
}

// 同步取职务：读取已缓存记录，未加载或未命中时返回普通同学
function getPositionFor(studentId) {
  const rec = rosterCache[String(studentId || "").trim()];
  return (rec && rec.position) || "普通同学";
}

// 异步取职务：确保该学号记录已加载后再匹配
function matchPosition(studentId) {
  const id = String(studentId || "").trim();
  return loadStudent(id).then(() => getPositionFor(id));
}

// 取某学号担任课代表的科目 key 列表（同步，需先 loadStudent/matchPosition 等）
function getRepSubjects(studentId) {
  const rec = rosterCache[String(studentId || "").trim()];
  return rec && Array.isArray(rec.repSubjects) ? rec.repSubjects : [];
}

// 异步取课代表科目：确保该学号记录已加载后返回
function matchRepSubjects(studentId) {
  const id = String(studentId || "").trim();
  return loadStudent(id).then(() => getRepSubjects(id));
}

// 当前登录用户是否为某科目课代表
function isSubjectRep(subjectKey) {
  const u = getApp().globalData.userInfo || {};
  return loadStudent(u.studentId).then(() => getRepSubjects(u.studentId).includes(subjectKey));
}

function validateProfile(form = {}) {
  if (!String(form.name || "").trim()) return "请填写姓名";
  if (!String(form.studentId || "").trim()) return "请填写学号";
  if (!String(form.className || "").trim()) return "请填写班级";
  if (!form.position) return "请选择职位";
  return "";
}

function saveProfile(form) {
  const app = getApp();
  return api
    .call("register", {
      name: String(form.name).trim(),
      studentId: String(form.studentId).trim(),
      className: String(form.className).trim(),
      position: form.position,
      // 课代表由管理员在课程设置中统一指定，登记时不可自选
    })
    .then((r) => {
      if (!r || !r.ok) return Promise.reject((r && r.msg) || "登记失败");
      app.updateUser(r.user);
      return r.user;
    });
}

module.exports = {
  DEFAULT_CLASS,
  loadStudent,
  getPositionFor,
  matchPosition,
  getRepSubjects,
  matchRepSubjects,
  isSubjectRep,
  validateProfile,
  saveProfile,
};
