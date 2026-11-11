// SQLite 文档存储：保持与原云开发集合一致的文档结构（_id / _openid / 毫秒时间戳）
// 数据量是班级规模，查询在内存中过滤即可；如需更大规模可在此层替换为 MongoDB。
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const Database = require("better-sqlite3");
const config = require("./config");

fs.mkdirSync(config.dataDir, { recursive: true });
const dbFile = path.join(config.dataDir, "huiyingzhai.sqlite");
const db = new Database(dbFile);

db.pragma("journal_mode = WAL");
db.pragma("synchronous = NORMAL");
db.exec(`
  CREATE TABLE IF NOT EXISTS documents (
    collection TEXT NOT NULL,
    id         TEXT NOT NULL,
    data       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (collection, id)
  );
  CREATE INDEX IF NOT EXISTS idx_documents_collection ON documents(collection);
  CREATE INDEX IF NOT EXISTS idx_documents_created ON documents(collection, created_at);

  CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    openid     TEXT NOT NULL,
    expire_at  INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_sessions_expire ON sessions(expire_at);
`);

const stmts = {
  insert: db.prepare(
    "INSERT INTO documents (collection, id, data, created_at) VALUES (?, ?, ?, ?)"
  ),
  get: db.prepare("SELECT data FROM documents WHERE collection = ? AND id = ?"),
  all: db.prepare("SELECT data FROM documents WHERE collection = ?"),
  update: db.prepare("UPDATE documents SET data = ? WHERE collection = ? AND id = ?"),
  remove: db.prepare("DELETE FROM documents WHERE collection = ? AND id = ?"),

  insertSession: db.prepare(
    "INSERT INTO sessions (token_hash, openid, expire_at) VALUES (?, ?, ?)"
  ),
  getSession: db.prepare("SELECT token_hash, openid, expire_at FROM sessions WHERE token_hash = ?"),
  deleteSession: db.prepare("DELETE FROM sessions WHERE token_hash = ?"),
  cleanupSessions: db.prepare("DELETE FROM sessions WHERE expire_at < ?"),
};

const sha256 = (value) => crypto.createHash("sha256").update(String(value)).digest("hex");

function parse(row) {
  try {
    return JSON.parse(row.data);
  } catch (e) {
    return null;
  }
}

function all(collection) {
  return stmts.all
    .all(collection)
    .map(parse)
    .filter(Boolean);
}

// 通用查询：predicate 可选，sortBy 支持文档字段，limit 截断
function find(collection, predicate, options = {}) {
  let list = all(collection);
  if (typeof predicate === "function") list = list.filter(predicate);
  if (options.sortBy) {
    const dir = options.direction === "asc" ? 1 : -1;
    list.sort((a, b) => {
      const av = a[options.sortBy];
      const bv = b[options.sortBy];
      if (av === bv) return 0;
      if (av === undefined || av === null) return 1;
      if (bv === undefined || bv === null) return -1;
      return av < bv ? -dir : dir;
    });
  }
  if (options.limit && options.limit > 0) list = list.slice(0, options.limit);
  return list;
}

function findOne(collection, predicate) {
  return find(collection, predicate)[0] || null;
}

function get(collection, id) {
  if (!id) return null;
  const row = stmts.get.get(collection, String(id));
  return row ? parse(row) : null;
}

function insert(collection, doc = {}) {
  const id = doc._id || crypto.randomUUID();
  const data = { ...doc, _id: id };
  stmts.insert.run(collection, id, JSON.stringify(data), Date.now());
  return data;
}

function update(collection, id, patch) {
  const current = get(collection, id);
  if (!current) return null;
  const next = { ...current, ...patch, _id: current._id };
  stmts.update.run(JSON.stringify(next), collection, current._id);
  return next;
}

function remove(collection, id) {
  if (!id) return false;
  return stmts.remove.run(collection, String(id)).changes > 0;
}

function removeWhere(collection, predicate) {
  const targets = find(collection, predicate);
  const tx = db.transaction((ids) => {
    ids.forEach((id) => stmts.remove.run(collection, id));
    return ids.length;
  });
  return tx(targets.map((doc) => doc._id));
}

function count(collection, predicate) {
  return find(collection, predicate).length;
}

// ===== 登录会话 =====
function createSession(openid) {
  const token = crypto.randomBytes(32).toString("hex");
  const expireAt = Date.now() + config.sessionDays * 24 * 60 * 60 * 1000;
  stmts.insertSession.run(sha256(token), openid, expireAt);
  return token;
}

function getSessionOpenid(token) {
  if (!token) return null;
  const row = stmts.getSession.get(sha256(token));
  if (!row) return null;
  if (row.expire_at < Date.now()) {
    stmts.deleteSession.run(row.token_hash);
    return null;
  }
  return row.openid;
}

function cleanupSessions() {
  stmts.cleanupSessions.run(Date.now());
}

module.exports = {
  db,
  dbFile,
  all,
  find,
  findOne,
  get,
  insert,
  update,
  remove,
  removeWhere,
  count,
  createSession,
  getSessionOpenid,
  cleanupSessions,
};
