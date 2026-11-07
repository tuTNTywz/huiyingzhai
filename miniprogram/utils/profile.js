// 学籍登记共用逻辑：登记页与「我的」弹窗复用
// 职务与角色以数据库 roster 集合（学号名单）为准，自动根据学号确定

// 班级固定为本班，无需手动填写
const DEFAULT_CLASS = "人工智能26A3班";

// 名单缓存：学号 → 职务（首次使用时从数据库 roster 集合拉取）
let rosterCache = null;

function loadRoster() {
  if (rosterCache) return Promise.resolve(rosterCache);
  return wx.cloud
    .database()
    .collection("roster")
    .limit(100)
    .get()
    .then((res) => {
      rosterCache = {};
      (res.data || []).forEach((r) => {
        if (r.studentId) rosterCache[String(r.studentId)] = r.position || "普通同学";
      });
      return rosterCache;
    })
    .catch(() => (rosterCache = rosterCache || {}));
}

// 同步取职务：读取已缓存名单，未加载或未命中时返回普通同学
function getPositionFor(studentId) {
  return (rosterCache && rosterCache[String(studentId || "").trim()]) || "普通同学";
}

// 异步取职务：确保名单已加载后再匹配
function matchPosition(studentId) {
  const id = String(studentId || "").trim();
  return loadRoster().then(() => getPositionFor(id));
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

module.exports = { DEFAULT_CLASS, loadRoster, getPositionFor, matchPosition, validateProfile, saveProfile };
