// 自建后端调用封装：登录态、业务 action、附件上传/下载
const { BASE_URL: RAW_BASE_URL } = require("../config");

// 容忍配置里误写末尾斜杠
const BASE_URL = String(RAW_BASE_URL || "").replace(/\/+$/, "");

const TOKEN_KEY = "api_token";
let loginTask = null;

function getToken() {
  return wx.getStorageSync(TOKEN_KEY) || "";
}

function setToken(token) {
  if (token) wx.setStorageSync(TOKEN_KEY, token);
  else wx.removeStorageSync(TOKEN_KEY);
}

function authHeader() {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// 基础 HTTP 请求：统一返回 { status, body }，网络错误 reject 中文提示
function request({ path, method = "GET", data, header }) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: BASE_URL + path,
      method,
      data,
      header: { "Content-Type": "application/json", ...(header || {}) },
      timeout: 20000,
      success: (res) => {
        resolve({
          status: res.statusCode,
          body: res.data && typeof res.data === "object" ? res.data : {},
        });
      },
      fail: () => reject("网络异常，请检查网络或服务器是否可用"),
    });
  });
}

// 微信登录：wx.login code 换自建 token
function doLogin() {
  return new Promise((resolve, reject) => {
    wx.login({
      success: (res) => {
        if (!res.code) return reject("微信登录失败");
        request({ path: "/api/login", method: "POST", data: { code: res.code } })
          .then(({ status, body }) => {
            if (status === 200 && body && body.ok) {
              setToken(body.token);
              resolve(body);
            } else {
              reject((body && body.msg) || "登录失败，请稍后重试");
            }
          })
          .catch(reject);
      },
      fail: () => reject("微信登录失败"),
    });
  });
}

function login() {
  if (loginTask) return loginTask;
  loginTask = doLogin().then(
    (res) => {
      loginTask = null;
      return res;
    },
    (err) => {
      loginTask = null;
      throw err;
    }
  );
  return loginTask;
}

function callInternal(action, data, allowRetry) {
  return request({
    path: "/api/action",
    method: "POST",
    data: { action, data: data || {} },
    header: authHeader(),
  }).then(({ status, body }) => {
    if (status === 401) {
      if (!allowRetry) throw (body && body.msg) || "登录已过期，请重新进入小程序";
      // token 失效：静默重新登录后重试一次
      return login().then(() => callInternal(action, data, false));
    }
    if (!body || typeof body !== "object") throw "服务器返回异常";
    return body;
  });
}

// 业务调用：与云函数保持一致的 { ok, msg, ... } 返回结构
function call(action, data) {
  return callInternal(action, data, true);
}

function uploadOne(file, dir) {
  return new Promise((resolve, reject) => {
    wx.uploadFile({
      url: `${BASE_URL}/api/upload`,
      filePath: file.tempPath,
      name: "file",
      formData: { dir: dir || "misc" },
      header: authHeader(),
      timeout: 120000,
      success: (res) => {
        let body = {};
        try {
          body = JSON.parse(res.data || "{}");
        } catch (e) {
          return reject("服务器返回异常");
        }
        if (res.statusCode === 401) {
          return login()
            .then(() => uploadOne(file, dir))
            .then(resolve, reject);
        }
        if (body.ok) resolve(body);
        else reject(body.msg || "附件上传失败");
      },
      fail: () => reject("附件上传失败，请检查网络"),
    });
  });
}

// 顺序上传，返回 [{ fileID, name, type, size }]
function upload(files, dir, onProgress) {
  const list = Array.isArray(files) ? files : [];
  const uploaded = [];
  const run = (index) => {
    if (index >= list.length) return Promise.resolve(uploaded);
    if (onProgress) onProgress(`正在上传附件 ${index + 1}/${list.length}`);
    return uploadOne(list[index], dir)
      .then((res) => {
        uploaded.push(res.fileID);
        return run(index + 1);
      })
      .catch((err) => {
        // 清理本次已上传的文件，避免产生孤儿附件
        if (uploaded.length) deleteFiles(uploaded).catch(() => {});
        throw err;
      });
  };
  return run(0).then((fileIDs) =>
    list.map((file, index) => ({
      fileID: fileIDs[index],
      name: file.name,
      type: file.type,
      size: file.size,
    }))
  );
}

function deleteFiles(fileList) {
  if (!Array.isArray(fileList) || !fileList.length) return Promise.resolve({ ok: true });
  return request({
    path: "/api/deleteFiles",
    method: "POST",
    data: { fileList },
    header: authHeader(),
  }).then(({ status, body }) => {
    if (status === 401) return login().then(() => deleteFiles(fileList));
    return body;
  });
}

// 附件相对路径 → 完整 URL
function fileUrl(fileID) {
  if (!fileID) return "";
  if (/^https?:\/\//i.test(fileID)) return fileID;
  return BASE_URL + (fileID.charAt(0) === "/" ? fileID : `/uploads/${fileID}`);
}

// 下载到本地临时文件（文档预览用）
function download(fileID) {
  return new Promise((resolve, reject) => {
    wx.downloadFile({
      url: fileUrl(fileID),
      timeout: 60000,
      success: (res) => {
        if (res.statusCode === 200) resolve(res.tempFilePath);
        else reject("下载失败");
      },
      fail: () => reject("下载失败"),
    });
  });
}

module.exports = {
  BASE_URL,
  getToken,
  setToken,
  login,
  call,
  upload,
  deleteFiles,
  fileUrl,
  download,
};
