// 附件上传与删除：文件保存在本地 uploads 目录，由 Nginx 直接对外提供
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const express = require("express");
const multer = require("multer");
const config = require("./config");
const { requireAuth } = require("./auth");

fs.mkdirSync(config.uploadDir, { recursive: true });

const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: config.maxUploadMb * 1024 * 1024, files: 1 },
});

// 目录只允许字母数字、下划线、中划线与斜杠，杜绝路径穿越
function safeDir(raw) {
  const cleaned = String(raw || "misc")
    .replace(/\\/g, "/")
    .split("/")
    .filter((part) => part && part !== "." && part !== "..")
    .join("/")
    .replace(/[^a-zA-Z0-9_\-/]/g, "")
    .replace(/\/{2,}/g, "/")
    .slice(0, 120);
  return cleaned || "misc";
}

// 部分客户端把 UTF-8 文件名按 latin1 传递，这里还原为中文
function fixOriginalName(raw) {
  const value = String(raw || "");
  if (!value || /[^\x00-\xff]/.test(value)) return value;
  const decoded = Buffer.from(value, "latin1").toString("utf8");
  return decoded.includes("�") ? value : decoded;
}

// 保留中文与常见文件名字符，去掉路径分隔符
function safeName(raw) {
  const base = path.basename(String(raw || "file"));
  const cleaned = base.replace(/[^\w.\-一-龥]/g, "_").slice(-120);
  return cleaned || "file";
}

async function saveUpload(req) {
  if (!req.file) throw new Error("未收到文件");
  const dir = safeDir(req.body && req.body.dir);
  const targetDir = path.join(config.uploadDir, dir);
  await fs.promises.mkdir(targetDir, { recursive: true });
  const originalName = fixOriginalName(req.file.originalname);
  const name = safeName(originalName);
  const filename = `${Date.now()}_${crypto.randomBytes(6).toString("hex")}_${name}`;
  await fs.promises.writeFile(path.join(targetDir, filename), req.file.buffer);
  return {
    fileID: `/uploads/${dir}/${filename}`,
    name: originalName || name,
    size: req.file.size,
    type: String(req.file.mimetype || "").startsWith("image/") ? "image" : "file",
  };
}

// 仅允许删除 uploads 目录内的文件
function resolveUploadPath(fileID) {
  const value = String(fileID || "");
  if (!value.startsWith("/uploads/")) return null;
  const root = path.resolve(config.uploadDir);
  const full = path.resolve(root, value.slice("/uploads/".length));
  if (full !== root && !full.startsWith(root + path.sep)) return null;
  return full;
}

async function deleteUploads(fileList) {
  const list = Array.isArray(fileList) ? fileList : [];
  await Promise.all(
    list.map(async (fileID) => {
      const full = resolveUploadPath(fileID);
      if (!full) return;
      try {
        await fs.promises.unlink(full);
      } catch (e) {
        if (e.code !== "ENOENT") console.error("删除附件失败", full, e.message);
      }
    })
  );
}

const router = express.Router();

router.post("/upload", requireAuth, (req, res) => {
  upload.single("file")(req, res, async (err) => {
    if (err) {
      const msg =
        err.code === "LIMIT_FILE_SIZE"
          ? `文件超过 ${config.maxUploadMb}MB 限制`
          : "附件上传失败";
      return res.json({ ok: false, msg });
    }
    try {
      const result = await saveUpload(req);
      res.json({ ok: true, ...result });
    } catch (e) {
      console.error("保存附件失败", e);
      res.json({ ok: false, msg: e.message || "附件上传失败" });
    }
  });
});

router.post("/deleteFiles", requireAuth, async (req, res) => {
  try {
    await deleteUploads(req.body && req.body.fileList);
    res.json({ ok: true });
  } catch (e) {
    res.json({ ok: false, msg: "附件删除失败" });
  }
});

module.exports = { router, deleteUploads, safeDir };
