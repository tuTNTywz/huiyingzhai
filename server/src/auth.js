// 微信登录：wx.login 的 code 换取 openid，签发并校验自建会话 token
const config = require("./config");
const store = require("./db");

// code 换 openid（开发模式不请求微信服务器）
async function code2session(code) {
  if (config.devMode) {
    return { openid: config.devOpenid, sessionKey: "", dev: true };
  }
  if (!config.appid || !config.secret) {
    throw new Error("服务端未配置微信凭据（WX_APPID / WX_SECRET）");
  }
  if (!code) throw new Error("缺少微信登录 code");
  const url =
    `${config.wxApiBase}/sns/jscode2session` +
    `?appid=${encodeURIComponent(config.appid)}` +
    `&secret=${encodeURIComponent(config.secret)}` +
    `&js_code=${encodeURIComponent(code)}` +
    `&grant_type=authorization_code`;
  let data;
  try {
    const resp = await fetch(url, { signal: AbortSignal.timeout(10000) });
    data = await resp.json();
  } catch (e) {
    throw new Error("微信登录服务不可用，请稍后重试");
  }
  if (!data.openid) {
    console.error("jscode2session 失败", data);
    throw new Error(data.errmsg || "微信登录失败，请稍后重试");
  }
  return { openid: data.openid, sessionKey: data.session_key || "" };
}

// 从 Authorization: Bearer <token> 中解析登录用户
function requireAuth(req, res, next) {
  const header = String(req.get("authorization") || "");
  const token = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
  const openid = store.getSessionOpenid(token);
  if (!openid) {
    return res.status(401).json({ ok: false, msg: "登录已过期，请重新进入小程序" });
  }
  const user = store.findOne("users", (u) => u._openid === openid);
  req.openid = openid;
  req.user = user;
  if (!user) {
    return res.status(401).json({ ok: false, msg: "账号档案不存在，请重新登录" });
  }
  next();
}

module.exports = { code2session, requireAuth };
