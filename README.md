# huiyingzhai

班级同窗小助手。作业发布、任务提醒、签到打卡，一册在手，班务无忧。汇聚英才，共砚同窗。

## 项目结构

```
.
├── miniprogram/        # 微信小程序前端
│   ├── config.js       # 后端地址、订阅消息模板 ID（部署前必改）
│   └── utils/api.js    # 自建后端调用封装（登录、action、附件）
├── server/             # Ubuntu 自建后端（Express + SQLite）
│   ├── src/            # 服务端代码
│   ├── deploy/         # systemd / Nginx / 安装脚本
│   └── README.md       # 部署与运维文档
├── cloudfunctions/     # 原云开发云函数（已不再使用，仅保留参考）
└── roster_import.json  # 学号名单（JSON Lines）
```

## 快速开始

### 后端

```bash
cd server
npm install
cp .env.example .env      # 本地调试请在 .env 中设置 DEV_MODE=true
npm run seed:roster ../roster_import.json
npm start
```

详细的生产部署步骤（Nginx、HTTPS、微信服务器域名、订阅消息）见 [server/README.md](server/README.md)。

### 小程序

1. 编辑 `miniprogram/config.js`，把 `BASE_URL` 改为后端 HTTPS 域名。
2. 用微信开发者工具导入本目录，编译运行。
3. 开发阶段可在「详情 → 本地设置」勾选「不校验合法域名」；正式发布前必须在公众平台配置 request / uploadFile / downloadFile 合法域名。

## 从云开发迁移

原实现直接使用云开发数据库、云函数、云存储。现在：

- 登录：`wx.login` code → 后端换取 openid → 自建 token 会话
- 读写：`/api/action`，服务端统一校验权限
- 附件：上传到服务器 `uploads/`，由 Nginx 提供访问
- 推送：服务端获取微信 access_token 后发送订阅消息

云开发中的旧数据不会自动迁移。学号名单可通过 `server/scripts/import-roster.js` 重新导入。
