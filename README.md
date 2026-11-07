# huiyingzhai
班级同窗小助手。作业发布、任务提醒、签到打卡，一册在手，班务无忧。汇聚英才，共砚同窗。

## 云开发配置

本项目使用微信云开发（云函数 + 云数据库）。首次运行前请按 **[cloudfunctions/README.md](cloudfunctions/README.md)** 完成：

1. 开通云开发并填写 `miniprogram/app.js` 中的 `env` 环境 ID；
2. 右键 `cloudfunctions/api` →「上传并部署：云端安装依赖」；
3. 将数据库各集合权限设为「所有用户可读，仅创建者可读写」。

部署后用 `wx.cloud.callFunction({ name: "api", data: { action: "ping" } })` 自检。
