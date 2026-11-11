// 导入学号名单到 SQLite（支持 JSON 数组或 JSON Lines）
// 用法：npm run seed:roster [文件路径]
// 默认读取仓库根目录的 roster_import.json
const fs = require("fs");
const path = require("path");
const config = require("../src/config");
const store = require("../src/db");

const file = path.resolve(
  process.argv[2] || path.join(config.root, "..", "roster_import.json")
);

function parseRows(text) {
  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) return parsed;
    if (parsed && Array.isArray(parsed.data)) return parsed.data;
    if (parsed && parsed.studentId) return [parsed];
  } catch (e) {
    // 不是合法 JSON，按 JSON Lines 处理
  }
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, index) => {
      try {
        return JSON.parse(line);
      } catch (e) {
        throw new Error(`第 ${index + 1} 行不是合法 JSON：${line.slice(0, 60)}`);
      }
    });
}

function main() {
  if (!fs.existsSync(file)) {
    console.error(`名单文件不存在：${file}`);
    process.exit(1);
  }
  const rows = parseRows(fs.readFileSync(file, "utf8"));
  let created = 0;
  let updated = 0;
  let skipped = 0;

  rows.forEach((row) => {
    const studentId = String((row && row.studentId) || "").trim();
    const name = String((row && row.name) || "").trim();
    if (!studentId || !name) {
      skipped += 1;
      return;
    }
    const position = String(row.position || "普通同学").trim() || "普通同学";
    const repSubjects = Array.isArray(row.repSubjects)
      ? row.repSubjects.map((s) => String(s).trim()).filter(Boolean)
      : [];

    const existing = store.findOne("roster", (r) => r.studentId === studentId);
    if (existing) {
      store.update("roster", existing._id, {
        name,
        position,
        repSubjects,
        updateTime: Date.now(),
      });
      updated += 1;
    } else {
      store.insert("roster", {
        studentId,
        name,
        position,
        repSubjects,
        createTime: Date.now(),
      });
      created += 1;
    }
  });

  console.log(`名单导入完成：新增 ${created}，更新 ${updated}，跳过 ${skipped}`);
  console.log(`数据库：${store.dbFile}`);
}

main();
