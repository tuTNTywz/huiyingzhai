// 工具函数
function formatTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const p = (n) => (n < 10 ? "0" + n : "" + n);
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 时间段：同一天省略结束日期，跨天显示完整结束时间
function formatTimeRange(startTs, endTs) {
  if (!startTs) return "";
  const s = new Date(startTs);
  const e = endTs ? new Date(endTs) : null;
  const p = (n) => (n < 10 ? "0" + n : "" + n);
  const datePart = (d) => `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  const start = `${datePart(s)} ${p(s.getHours())}:${p(s.getMinutes())}`;
  if (!e) return start;
  const sameDay = datePart(s) === datePart(e);
  const end = sameDay
    ? `${p(e.getHours())}:${p(e.getMinutes())}`
    : `${datePart(e)} ${p(e.getHours())}:${p(e.getMinutes())}`;
  return `${start} 至 ${end}`;
}

// 从云函数 serverDate 返回的时间对象中取毫秒时间戳
function toDateMs(v) {
  if (!v) return 0;
  if (typeof v === "number") return v;
  if (v instanceof Date) return v.getTime();
  if (typeof v === "string") return new Date(v.replace(/-/g, "/")).getTime() || 0;
  if (v.$date) return v.$date;
  return new Date(v).getTime() || 0;
}

module.exports = { formatTime, formatTimeRange, toDateMs };
