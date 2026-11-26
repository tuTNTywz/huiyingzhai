# huiyingzhai

班级同窗小助手：作业、活动、志愿活动、交流与通知。汇聚英才，共砚同窗。

## 项目结构

```
.
├── server/             # Ubuntu 自建后端（Python + FastAPI + SQLite）
│   ├── app/            # 服务端代码
│   ├── deploy/         # systemd / Nginx / 安装脚本 / 网页一键部署面板
│   └── README.md       # 部署与运维文档
└── roster_import.json  # 学号名单（JSON Lines）
```

## 快速开始

### 后端

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # 修改 BASE_URL 等
python scripts/import_roster.py ../roster_import.json
python -m app
```

详细部署步骤（Nginx、HTTPS、备份、通知机器人）见 [server/README.md](server/README.md)。

### 网页

- `https://<域名>/huiyingzhai/`：班级内容网页版，使用姓名 + 学号登录；
- `https://<域名>/upload/`：班委/课代表发布作业、活动、志愿活动、学科任务；
- `https://<域名>/deploy/`：管理员网页部署控制台。

网页登录按班级名单校验姓名 + 学号；名单内学号首次登录自动创建网页身份，不需要单独注册。

## 数据

- 数据库：`server/data/huiyingzhai.sqlite`
- 附件：`server/uploads/`
- 学号名单：通过 `server/scripts/import_roster.py` 导入
- 自定义介绍/说明书页面：`/var/lib/huiyingzhai-panel/pages/`
