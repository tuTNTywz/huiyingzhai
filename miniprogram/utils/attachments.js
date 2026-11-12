// 附件通用逻辑：文件 / 照片的选择、上传与查看（作业、板块、交流共用）
const api = require("./api");
const { formatSize } = require("./util");

const MAX_FILES = 5;
const MAX_SIZE = 20 * 1024 * 1024;
const DOC_EXT = ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt"];
const IMAGE_EXT = ["jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif"];

// 判断是否为照片：type 字段可能因历史数据或云函数版本缺失，按文件名兜底
function isImage(file) {
  const f = file || {};
  if (f.type === "image") return true;
  const name = String(f.name || "");
  const ext = name.indexOf(".") >= 0 ? name.split(".").pop().toLowerCase() : "";
  return IMAGE_EXT.indexOf(ext) >= 0 || /^(照片|图片)[_\-]/.test(name);
}

// 从数据库读出的附件补上展示用的大小文案，并把 type 补齐供页面判断图标/文案
function decorate(attachments) {
  return (attachments || []).map((a) => ({
    ...a,
    type: isImage(a) ? "image" : a.type || "file",
    sizeText: a.size ? formatSize(a.size) : "",
  }));
}

// 低版本基础库或开发者工具可能没有这些 API，统一做能力检测，避免框架层抛 unsupport
function unsupportedTip(text) {
  wx.showToast({ title: text, icon: "none", duration: 2000 });
}

// 文档从微信聊天记录选择
function pickFiles(remain) {
  if (typeof wx.chooseMessageFile !== "function") {
    unsupportedTip("当前环境不支持选文件，请用真机调试");
    return Promise.resolve([]);
  }
  return new Promise((resolve) => {
    try {
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
    } catch (e) {
      unsupportedTip("当前环境不支持选文件，请用真机调试");
      resolve([]);
    }
  });
}

// 照片从相册或拍照添加；chooseMedia 不可用时回退到 chooseImage
function pickImages(remain) {
  return new Promise((resolve) => {
    const toFiles = (raw) =>
      (raw || [])
        .map((f, i) => ({
          name: `照片_${Date.now()}_${i + 1}.jpg`,
          size: (f && f.size) || 0,
          tempPath:
            (f && (f.tempFilePath || f.path)) || (typeof f === "string" ? f : ""),
          type: "image",
        }))
        .filter((f) => f.tempPath);

    try {
      if (typeof wx.chooseMedia === "function") {
        wx.chooseMedia({
          count: remain,
          mediaType: ["image"],
          sourceType: ["album", "camera"],
          success: (res) => resolve(toFiles(res.tempFiles)),
          fail: () => resolve([]),
        });
        return;
      }
      if (typeof wx.chooseImage === "function") {
        wx.chooseImage({
          count: remain,
          sourceType: ["album", "camera"],
          success: (res) => resolve(toFiles(res.tempFiles || res.tempFilePaths)),
          fail: () => resolve([]),
        });
        return;
      }
      unsupportedTip("当前环境不支持选照片，请升级微信");
      resolve([]);
    } catch (e) {
      unsupportedTip("当前环境不支持选照片，请用真机调试");
      resolve([]);
    }
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

// 查看附件：统一先下载到本地临时文件，再按类型预览
// 先下载再预览，避免真机上远程地址因签名/缓存差异导致 wx.previewImage 加载失败
function open(file) {
  const f = file || {};
  if (!f.fileid) return Promise.resolve();
  wx.showLoading({ title: "加载中…", mask: true });
  const done = () => wx.hideLoading();

  const preview = (path) => {
    wx.previewImage({
      current: path,
      urls: [path],
      fail: () => wx.showToast({ title: "照片预览失败", icon: "none" }),
    });
  };

  return api
    .download(f.fileid)
    .then((tempFilePath) => {
      done();
      const path = tempFilePath;
      if (!path) {
        wx.showToast({ title: "附件加载失败", icon: "none" });
        return;
      }
      if (isImage(f)) {
        preview(path);
        return;
      }
      const ext = String(f.name || "").split(".").pop().toLowerCase();
      wx.openDocument({
        filePath: path,
        fileType: ext,
        showMenu: true,
        fail: () => {
          // 类型标记丢失但实为图片时，再按图片预览一次
          if (IMAGE_EXT.indexOf(ext) >= 0) {
            preview(path);
            return;
          }
          wx.saveFile({
            tempFilePath: path,
            success: () => wx.showToast({ title: "已保存，可在文件管理中查看" }),
            fail: () => wx.showToast({ title: "该类型暂不支持预览", icon: "none" }),
          });
        },
      });
    })
    .catch((err) => {
      done();
      console.error("[附件] 下载失败", err);
      wx.showToast({ title: "附件下载失败，请稍后重试", icon: "none" });
    });
}

module.exports = { MAX_FILES, DOC_EXT, IMAGE_EXT, decorate, isImage, pickFiles, pickImages, merge, upload, open };
