// 服务端配置：全部来自 server/.env（参考 .env.example）
const path = require("path");
require("dotenv").config({ path: path.join(__dirname, "..", ".env"), quiet: true });

const root = path.join(__dirname, "..");

function bool(value, fallback) {
  if (value === undefined || value === "") return fallback;
  return /^(1|true|yes|on)$/i.test(String(value));
}

const appid = String(process.env.WX_APPID || "").trim();
const secret = String(process.env.WX_SECRET || "").trim();

module.exports = {
  root,
  port: Number(process.env.PORT) || 3000,
  // 对外访问地址（小程序服务器域名 / 反向代理域名），无末尾斜杠
  baseUrl: String(process.env.BASE_URL || "").trim().replace(/\/+$/, ""),

  // 微信小程序凭据：在微信公众平台「开发管理 - 开发设置」获取
  appid,
  secret,
  // 开发模式需显式开启：不发真实请求，所有登录固定为 DEV_OPENID
  // 生产环境必须保持关闭，避免所有用户被识别成同一账号
  devMode: bool(process.env.DEV_MODE, false),
  devOpenid: String(process.env.DEV_OPENID || "dev-openid-1").trim() || "dev-openid-1",

  // 登录有效期（天）
  sessionDays: Number(process.env.SESSION_DAYS) || 30,

  // 订阅消息模板 ID 与推送版本：developer / trial / formal
  homeworkTmplId: String(process.env.WX_TMPL_HOMEWORK || "6avlHCT0IR-fd1hk3c15hQDn0X0VWmuQs0rcdbN5FgM").trim(),
  pushState: String(process.env.WX_PUSH_STATE || "trial").trim(),

  // 数据与附件目录，默认放在 server/data、server/uploads
  dataDir: process.env.DATA_DIR ? path.resolve(process.env.DATA_DIR) : path.join(root, "data"),
  uploadDir: process.env.UPLOAD_DIR ? path.resolve(process.env.UPLOAD_DIR) : path.join(root, "uploads"),

  maxUploadMb: Number(process.env.MAX_UPLOAD_MB) || 20,

  // 微信接口地址（测试时可替换为 mock）
  wxApiBase: String(process.env.WX_API_BASE || "https://api.weixin.qq.com").replace(/\/+$/, ""),
};
