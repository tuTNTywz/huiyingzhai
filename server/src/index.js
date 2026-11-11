// 汇英斋自建后端入口：Express + SQLite
const fs = require("fs");
const path = require("path");
const express = require("express");
const config = require("./config");
const store = require("./db");
const { code2session, requireAuth } = require("./auth");
const { handleAction } = require("./api");
const files = require("./files");

fs.mkdirSync(config.uploadDir, { recursive: true });
store.cleanupSessions();

const app = express();
app.disable("x-powered-by");
app.set("trust proxy", true);

app.use((req, res, next) => {
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("Referrer-Policy", "no-referrer");
  next();
});

// 附件静态目录：图片可直接预览，其它类型强制下载，避免被当作网页执行
app.use(
  "/uploads",
  (req, res, next) => {
    const ext = path.extname(req.path).toLowerCase();
    const inline = [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"].includes(ext);
    if (!inline) res.setHeader("Content-Disposition", "attachment");
    next();
  },
  express.static(config.uploadDir, { maxAge: "7d", index: false, dotfiles: "deny" })
);

app.use(express.json({ limit: "1mb" }));

app.get("/api/health", (req, res) => {
  res.json({
    ok: true,
    time: Date.now(),
    baseUrl: config.baseUrl || null,
    devMode: config.devMode,
    push: !!(config.homeworkTmplId && config.appid && config.secret),
  });
});

// 微信登录：code 换 openid，首次登录自动建档
app.post("/api/login", async (req, res) => {
  try {
    const { openid } = await code2session(req.body && req.body.code);
    let user = store.findOne("users", (u) => u._openid === openid);
    if (!user) {
      user = store.insert("users", {
        _openid: openid,
        nickname: "同窗",
        role: "student",
        registered: false,
        createTime: Date.now(),
      });
    }
    store.cleanupSessions();
    const token = store.createSession(openid);
    res.json({ ok: true, token, user, registered: !!user.registered });
  } catch (e) {
    console.error("登录失败", e.message || e);
    res.json({ ok: false, msg: e.message || "登录失败，请稍后重试" });
  }
});

// 业务接口：与小程序端约定 { action, data }
app.post("/api/action", requireAuth, async (req, res) => {
  const { action, data } = req.body || {};
  if (!action) return res.json({ ok: false, msg: "缺少 action" });
  try {
    const result = await handleAction(req.user, String(action), data || {});
    res.json(result);
  } catch (e) {
    console.error("api error", action, e);
    res.json({ ok: false, msg: e.message || "服务异常，请稍后重试" });
  }
});

// 附件上传 / 删除
app.use("/api", files.router);

app.use((req, res) => {
  res.status(404).json({ ok: false, msg: "接口不存在" });
});

app.use((err, req, res, next) => {
  console.error("未处理异常", err);
  if (res.headersSent) return next(err);
  res.status(500).json({ ok: false, msg: "服务异常，请稍后重试" });
});

const server = app.listen(config.port, () => {
  console.log(`汇英斋服务已启动：http://127.0.0.1:${config.port}`);
  console.log(`对外地址：${config.baseUrl || "未配置（请填写 BASE_URL）"}`);
  console.log(`数据目录：${config.dataDir}`);
  console.log(`附件目录：${config.uploadDir}`);
  if (config.devMode) {
    console.log(`当前为开发模式：登录固定使用 openid=${config.devOpenid}（不会请求微信服务器）`);
  }
});

function shutdown(signal) {
  console.log(`收到 ${signal}，正在关闭服务…`);
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 5000).unref();
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
