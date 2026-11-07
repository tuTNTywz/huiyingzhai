// 统一身份标签：以 roster 名单为准，生成「管理员 / 职务 / 课代表·科目 / 同学」标签
// 各处人员信息（发帖、回复、报名、发布人）共用同一套口径
const { loadRoster, findRosterByIdentity } = require("./profile");
const { normalizeSubjectKeys, extractRepSubjects, shortSubjectName } = require("./subjects");

function tagsFor(record = {}) {
  const name = record.name || record.nickname || record.creatorName || "";
  const rec = findRosterByIdentity({ name, studentId: record.studentId });
  const tags = [];

  if (rec) {
    const position = String(rec.position || "普通同学").trim();
    if (position === "管理员") {
      tags.push({ text: "管理员", cls: "tag-red" });
    } else if (position && position !== "普通同学" && position !== "同学") {
      tags.push({ text: position, cls: "tag-gold" });
    }
    // 课代表按负责科目逐个生成标签，与名单一一对应
    normalizeSubjectKeys(extractRepSubjects(rec)).forEach((k) => {
      tags.push({ text: `课代表·${shortSubjectName(k)}`, cls: "tag-gold" });
    });
  } else {
    // 名单未命中时回退到记录里已存的 role
    if (record.role === "admin") tags.push({ text: "管理员", cls: "tag-red" });
    else if (record.role === "committee") tags.push({ text: "班委", cls: "tag-gold" });
  }

  if (!tags.length) tags.push({ text: "同学", cls: "tag-green" });
  return tags;
}

// 列表批量附加 identityTags
function withIdentity(list = []) {
  return loadRoster()
    .then(() => list.map((item) => ({ ...item, identityTags: tagsFor(item) })))
    .catch(() => list);
}

// 单个对象附加 identityTags
function withIdentityOne(record = {}) {
  return loadRoster()
    .then(() => ({ ...record, identityTags: tagsFor(record) }))
    .catch(() => record);
}

module.exports = { tagsFor, withIdentity, withIdentityOne };
