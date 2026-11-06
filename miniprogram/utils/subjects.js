// 九个学科板块（「大学英语」在需求中重复出现，按九个板块去重）
const SUBJECTS = [
  { key: "english", name: "大学英语", ordinal: "壹" },
  { key: "math", name: "高等数学", ordinal: "贰" },
  { key: "psychology", name: "大学生心理健康教育", ordinal: "叁" },
  { key: "ai", name: "人工智能基础及应用", ordinal: "肆" },
  { key: "physics", name: "大学物理", ordinal: "伍" },
  { key: "defense", name: "国防教育", ordinal: "陆" },
  { key: "ideology", name: "思想道德与法治", ordinal: "柒" },
  { key: "pe", name: "体育", ordinal: "捌" },
  { key: "career", name: "大学生职业生涯发展与规划", ordinal: "玖" },
];

const SUBJECT_KEYS = SUBJECTS.map((s) => s.key);

function getSubject(key) {
  return SUBJECTS.find((s) => s.key === key) || null;
}

function subjectName(key) {
  const s = getSubject(key);
  return s ? s.name : "";
}

module.exports = { SUBJECTS, SUBJECT_KEYS, getSubject, subjectName };
