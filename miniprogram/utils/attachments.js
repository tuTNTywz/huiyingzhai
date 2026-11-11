// 附件通用逻辑：文件 / 照片的选择、上传与查看（作业、板块、交流共用）
const api = require("./api");
const { formatSize } = require("./util");

const MAX_FILES = 5;
const MAX_SIZE = 20 * 1024 * 1024;
const DOC_EXT = ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt"];

// 从数据库读出的附件补上展示用的大小文案
function decorate(attachments) {
  return (attachments || []).map((a) => ({
    ...a,
    sizeText: a.size ? formatSize(a.size) : "",
  }));
}

// 文档从微信聊天记录选择
function pickFiles(remain) {
  return new Promise((resolve) => {
    wx.chooseMessageFile({
      count: remain,
      type: "file",
      extension: DOC_EXT,
      success: (res) =>
        resolve(
          (res.tempFiles || []).map((f) => ({
            name: f.name,
            size: f.size,
            tempPath: f.path,
            type: "file",
          }))
        ),
      fail: () => resolve([]),
    });
  });
}

// 照片从相册或拍照添加
function pickImages(remain) {
  return new Promise((resolve) => {
    wx.chooseMedia({
      count: remain,
      mediaType: ["image"],
      sourceType: ["album", "camera"],
      success: (res) =>
        resolve(
          (res.tempFiles || []).map((f, i) => ({
            name: `照片_${Date.now()}_${i + 1}.jpg`,
            size: f.size,
            tempPath: f.tempFilePath,
            type: "image",
          }))
        ),
      fail: () => resolve([]),
    });
  });
}

// 合并已选附件，校验数量与单个大小
function merge(current, picked) {
  const list = current.slice();
  const room = MAX_FILES - list.length;
  picked.forEach((f) => {
    if (list.length >= MAX_FILES) return;
    if (f.size > MAX_SIZE) {
      wx.showToast({ title: `${f.name} 超过 20MB`, icon: "none" });
      return;
    }
    list.push({ ...f, sizeText: formatSize(f.size) });
  });
  if (picked.length > room) {
    wx.showToast({ title: `最多添加 ${MAX_FILES} 个附件`, icon: "none" });
  }
  return list;
}

// 顺序上传到服务器；中途失败会由 api.upload 清理已上传文件
function upload(files, dir, onProgress) {
  return api.upload(files, dir, onProgress);
}

// 查看附件：照片全屏预览，文档用微信内置打开（可另存 / 转发）
function open(file) {
  const { fileid, name, type } = file;
  if (!fileid) return Promise.resolve();

  if (type === "image") {
    return new Promise((resolve) => {
      wx.previewImage({
        urls: [api.fileUrl(fileid)],
        fail: () => wx.showToast({ title: "照片加载失败", icon: "none" }),
        complete: resolve,
      });
    });
  }

  wx.showLoading({ title: "加载中…", mask: true });
  const done = () => wx.hideLoading();
  return api
    .download(fileid)
    .then((tempFilePath) => {
      done();
      const ext = (name || "").split(".").pop().toLowerCase();
      wx.openDocument({
        filePath: tempFilePath,
        fileType: ext,
        showMenu: true,
        fail: () => {
          wx.saveFile({
            tempFilePath,
            success: () => wx.showToast({ title: "已保存，可在文件管理中查看" }),
            fail: () => wx.showToast({ title: "该类型暂不支持预览", icon: "none" }),
          });
        },
      });
    })
    .catch(() => {
      done();
      wx.showToast({ title: "下载失败", icon: "none" });
    });
}

module.exports = { MAX_FILES, DOC_EXT, decorate, pickFiles, pickImages, merge, upload, open };
