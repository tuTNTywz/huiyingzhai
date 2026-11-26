# huiyingzhai

班级同窗小助手。作业发布、任务提醒、签到打卡，一册在手，班务无忧。汇聚英才，共砚同窗。

## 项目结构

```
.
├── server/             # Ubuntu 自建后端（Python + FastAPI + SQLite）
│   ├── app/            # 服务端代码
│   ├── deploy/         # systemd / Nginx / 安装脚本 / 网页一键部署面板
│   └── README.md       # 部署与运维文档
└── roster_import.json  # 学号名单（JSON Lines）
```

> 微信小程序客户端已移除；班级功能统一使用网站：
> `https://<域名>/huiyingzhai/`（作业、活动、志愿活动、交流、通知、我的）。

## 快速开始

### 后端

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # 本地调试请在 .env 中设置 DEV_MODE=true
python scripts/import_roster.py ../roster_import.json
python -m app
```

详细的生产部署步骤（Nginx、HTTPS、微信服务器域名、订阅消息）见 [server/README.md](server/README.md)。

### 网页

- `https://<域名>/huiyingzhai/`：班级内容网页版，使用姓名 + 学号登录；
- `https://<域名>/upload/`：班委/课代表发布作业、活动、志愿活动、学科任务；
- `https://<域名>/deploy/`：管理员网页部署控制台。

小程序前端代码已从仓库删除；后端仍保留原有登录/动作/订阅接口，网站不依赖微信客户端。

## 从云开发迁移

原实现直接使用云开发数据库、云函数、云存储。现在：

- 登录：`wx.login` code → 后端换取 openid → 自建 token 会话
- 读写：`/api/action`，服务端统一校验权限
- 附件：上传到服务器 `uploads/`，由 Nginx 提供访问
- 推送：服务端获取微信 access_token 后发送订阅消息

云开发中的旧数据不会自动迁移。学号名单可通过 `server/scripts/import_roster.py` 重新导入。
