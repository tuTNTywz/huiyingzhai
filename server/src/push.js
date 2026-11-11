// 微信订阅消息推送：access_token 缓存 + 逐个用户发送、扣减订阅次数
const config = require("./config");
const store = require("./db");

let tokenCache = { token: "", expireAt: 0 };

async function getAccessToken() {
  if (tokenCache.token && Date.now() < tokenCache.expireAt - 60 * 1000) {
    return tokenCache.token;
  }
  if (!config.appid || !config.secret) throw new Error("未配置 WX_APPID / WX_SECRET");
  const url =
    `${config.wxApiBase}/cgi-bin/token?grant_type=client_credential` +
    `&appid=${encodeURIComponent(config.appid)}&secret=${encodeURIComponent(config.secret)}`;
  const resp = await fetch(url, { signal: AbortSignal.timeout(10000) });
  const data = await resp.json();
  if (!data.access_token) {
    throw new Error(`获取 access_token 失败：${data.errcode || ""} ${data.errmsg || ""}`.trim());
  }
  tokenCache = {
    token: data.access_token,
    expireAt: Date.now() + (Number(data.expires_in) || 7200) * 1000,
  };
  return tokenCache.token;
}

// 订阅消息要求的日期格式：yyyy-MM-dd HH:mm
function noticeTimeText(ts) {
  const d = new Date(Number(ts) || Date.now());
  const p = (n) => (n < 10 ? "0" + n : "" + n);
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function sendOne(token, payload, subscription) {
  const resp = await fetch(
    `${config.wxApiBase}/cgi-bin/message/subscribe/send?access_token=${encodeURIComponent(token)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(10000),
    }
  );
  const result = await resp.json();
  if (result.errcode === 0) {
    store.update("subscriptions", subscription._id, {
      remain: Math.max(0, Number(subscription.remain || 1) - 1),
      updateTime: Date.now(),
    });
    return;
  }
  console.error("订阅消息推送失败", subscription._openid, result.errcode, result.errmsg);
  // 43101：用户未订阅 / 次数已用完；40003：openid 无效。清零避免持续重试
  if (result.errcode === 43101 || result.errcode === 40003) {
    store.update("subscriptions", subscription._id, { remain: 0, updateTime: Date.now() });
  }
}

// 模板字段：thing1=标题，time4=截止时间，thing5=备注（作业与学科任务共用）
async function pushSubscribeMessage({ page, title, time, remark, excludeOpenid }) {
  if (!config.homeworkTmplId || !config.appid || !config.secret) return;
  try {
    const subscriptions = store.find("subscriptions", (s) => Number(s.remain) > 0, {
      limit: 1000,
    });
    const targets = subscriptions.filter((s) => s._openid && s._openid !== excludeOpenid);
    if (!targets.length) return;

    let token;
    try {
      token = await getAccessToken();
    } catch (e) {
      console.error("推送流程异常：", e.message);
      return;
    }

    const payload = {
      template_id: config.homeworkTmplId,
      page,
      miniprogram_state: config.pushState,
      lang: "zh_CN",
      data: {
        thing1: { value: String(title || "新通知").slice(0, 20) },
        time4: { value: noticeTimeText(time) },
        thing5: { value: String(remark || "").trim().slice(0, 20) || "点击查看详情" },
      },
    };

    await Promise.all(
      targets.map((subscription) =>
        sendOne(token, { ...payload, touser: subscription._openid }, subscription).catch((e) =>
          console.error("推送请求失败", subscription._openid, e.message)
        )
      )
    );
  } catch (e) {
    console.error("推送流程异常", e.message || e);
  }
}

module.exports = { pushSubscribeMessage, getAccessToken };
