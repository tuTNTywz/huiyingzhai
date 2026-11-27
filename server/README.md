# 汇英斋后端（Ubuntu / Python）

班级作业、活动、志愿活动、交流与通知的网站后端：**Python 3 + FastAPI + SQLite**，可部署在任意 Ubuntu 云服务器上。

- 网页版：`https://<域名>/huiyingzhai/`
- 发布页：`https://<域名>/upload/`（班委 / 课代表）
- 部署控制台：`https://<域名>/deploy/`（管理员）
- 登录：班级名单中的 **姓名 + 学号**；名单内学号首次登录自动创建网页身份
- 权限：管理员、班委、课代表、普通同学，服务端统一校验

## 目录结构

```
server/
├── app/
│   ├── main.py      # FastAPI 入口与路由
│   ├── api.py       # 业务 action
│   ├── auth.py      # 会话解析
│   ├── config.py    # 环境变量
│   ├── db.py        # SQLite 文档存储
│   ├── files.py     # 附件上传/删除
│   ├── qqbot.py     # QQ 官方机器人 Webhook 与发送
│   └── robot.py     # PushPlus / 企业微信 / QQ 通知
├── deploy/
│   ├── install.sh             # 一键部署
│   ├── deploy_panel.py        # 主页 + 部署控制台
│   ├── huiyingzhai-deploy.sh  # root 部署脚本
│   ├── nginx.conf             # 站点配置示例
│   ├── *.service / *.path     # systemd 单元
│   └── huiyingzhai_web.html / deploy_page.html
├── scripts/import_roster.py   # 学号名单导入
└── data/ uploads/             # 运行时数据（不提交）
```

## 本地运行

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # 修改 BASE_URL 等
python scripts/import_roster.py ../roster_import.json
python -m app
```

自检：

```bash
curl http://127.0.0.1:3000/api/health
```

## 生产部署

### 1. 一键初始化

在仓库根目录：

```bash
sudo bash server/deploy/install.sh
```

脚本会安装 Nginx/Python、同步代码到 `/opt/huiyingzhai/server`、创建虚拟环境、注册后端/面板/部署服务，并生成网页登录共享密钥。

### 2. 环境变量

```bash
sudo nano /opt/huiyingzhai/server/.env
```

至少填写：

```bash
BASE_URL=https://你的域名
```

通知机器人按需填写（见下文）。`.env` 权限保持 600，不要提交到仓库。

### 3. Nginx 与 HTTPS

```bash
sudo cp /opt/huiyingzhai/server/deploy/nginx.conf /etc/nginx/sites-available/huiyingzhai
sudo nano /etc/nginx/sites-available/huiyingzhai   # 替换 api.example.com
sudo ln -s /etc/nginx/sites-available/huiyingzhai /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d 你的域名
```

站点需要包含 `/`、`/huiyingzhai/`、`/upload/`、`/deploy/`、`/intro/`、`/manual/`、`/me/`、`/api/` 和 `/uploads/` 的反代，`deploy/nginx.conf` 已包含完整示例。

### 4. 学号名单

```bash
cd /opt/huiyingzhai/server
sudo -u huiyingzhai .venv/bin/python scripts/import_roster.py /path/to/roster.json
```

按学号 upsert，可重复执行；JSON 数组和 JSON Lines 都支持。网页登录和发布权限都以名单为准。

## 网页功能

### 登录与身份

- 打开 `/huiyingzhai/` 或 `/upload/`，填写名单中的姓名 + 学号；
- 名单内学号首次登录自动创建网页身份，无需注册；
- 姓名不匹配或学号不在名单会被拒绝；
- 角色由名单职务决定：管理员/班长 → admin，其他班委 → committee，普通同学 → student。

### 栏目

| 栏目 | 内容 |
| --- | --- |
| 作业 | 班级作业与 9 个学科板块、附件、个人完成状态切换、删除（管理员/学习委员） |
| 活动 | 班级活动列表/详情、删除（班委/管理员） |
| 志愿活动 | 开始/结束时间、详细内容、删除（班委/管理员） |
| 交流 | 发帖、回复、附件 |
| 通知 | 班委/管理员发布的通知全文与附件；按发布时间倒序；保留 30 天后自动删除 |
| 我的 | 身份资料、上传入口、使用说明书、退出 |

### 发布内容

`/upload/` 按身份显示表单：

| 身份 / 职务 | 可发布 |
| --- | --- |
| 管理员 | 作业、活动、志愿活动、全部学科任务 |
| 学习委员 | 作业、活动、志愿活动、本人负责科目的任务 |
| 其他班委 | 活动、志愿活动、本人负责科目的任务 |
| 课代表 | 本人负责科目的任务 |
| 普通同学 | 不可发布 |

附件最多 5 个、单个 ≤20MB。

### 网页部署控制台

管理员打开 `/deploy/`，用姓名 + 学号登录：

1. 从 GitHub 下载当前分支源码；
2. 备份当前版本（保留最近 5 份）；
3. 同步 `server/`，**保留** `.env`、`data/`、`uploads/`、`.venv`；
4. `requirements.txt` 变化时执行 `pip install`；
5. 重启后端并轮询 `/api/health`，失败自动回滚。

部署接口同时要求管理员角色和 CSRF Token，非管理员返回 403。

### 页面编辑

管理员登录 `/upload/` 后，打开 `/intro/` 或 `/manual/`，右下角出现「页面编辑」工具条：下载当前 HTML、修改后上传，页面立即更新。自定义页面保存在 `/var/lib/huiyingzhai-panel/pages/`。

## 班级通知机器人

网页版自身已有站内通知。若还要推送微信/QQ，可在 `.env` 配置以下任一渠道：

### PushPlus 群组（微信）

1. 微信登录 <https://www.pushplus.plus/>，完成实名认证；
2. 「发送消息 → 一对多消息」新建群组，记下 token 和群组编码；
3. 配置并重启：

   ```bash
   sudo /usr/local/sbin/huiyingzhai-pushplus-setup
   sudo systemctl restart huiyingzhai
   ```

4. 把群组二维码发给同学扫码加入。免费版单群 100 人、每天 200 次。

### QQ 官方机器人

1. 在 <https://q.qq.com/> 创建机器人，获取 AppID、AppSecret（Bot Secret 与 AppSecret 相同）；
2. 回调地址：`https://<域名>/api/qq/webhook`；
3. `.env` 配置：

   ```bash
   QQ_APP_ID=
   QQ_APP_SECRET=
   QQ_BOT_SECRET=
   QQ_GROUP_OPENID=        # 可不填，入群事件会自动记录
   ```

4. 重启后端；把机器人拉进群，群主在手机 QQ 开启「主动消息」；
5. 在群里 @ 机器人一次，后端记录 `group_openid` 到 `data/qq_group.json`。

限制：主动推送默认按群关闭；单群约 20 条/分钟、每天 1000 条；消息约 1800 字，超长会截断。

### 企业微信群机器人（需全员企业微信）

企业微信外部群不支持群机器人；仅内部群可用。配置 `WECHAT_ROBOT_WEBHOOK` 后，发布作业/活动/志愿活动/学科任务会推送到该群。

## 日常运维

### 更新代码

```bash
cd /opt/huiyingzhai/server
sudo systemctl restart huiyingzhai
```

推荐直接用管理员网页 `/deploy/` 一键部署；修改 systemd/安装文件后需重跑 `install.sh`。

### 日志

```bash
sudo journalctl -u huiyingzhai -f
sudo journalctl -u huiyingzhai-panel -f
sudo tail -f /var/log/huiyingzhai-deploy.log
```

### 备份

```bash
sudo systemctl stop huiyingzhai
sudo sqlite3 /opt/huiyingzhai/server/data/huiyingzhai.sqlite ".backup '/root/huiyingzhai-$(date +%F).sqlite'"
sudo systemctl start huiyingzhai
sudo tar czf /root/uploads-$(date +%F).tar.gz -C /opt/huiyingzhai/server uploads
sudo tar czf /root/huiyingzhai-panel-$(date +%F).tar.gz -C /var/lib/huiyingzhai-panel .
```

## 接口说明

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| POST | `/api/web/token` | 面板用共享密钥换取名单学号的后端会话 |
| POST | `/api/action` | 业务 action，需 `Authorization: Bearer <token>` |
| POST | `/api/upload` | 附件上传，需登录，multipart 字段 `file` |
| GET | `/uploads/...` | 附件静态访问 |
| POST | `/api/qq/webhook` | QQ 官方机器人回调（Ed25519 验签） |

业务 action：`listHomeworks`、`getHomework`、`publishHomework`、`deleteHomework`、`setHomeworkStatus`、`listActivities`、`getActivity`、`publishActivity`、`deleteActivity`、`listVolunteers`、`publishVolunteer`、`listSubjectTasks`、`getSubjectSettings`、`getSubjectRep`、`publishSubjectTask`、`deleteSubjectTask`、`publishNotice`、`deleteNotice`、`fetchHomeworkNotices`、`listPosts`、`getPost`、`listReplies`、`createPost`、`createReply`。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 登录提示“姓名与学号不匹配” | 确认姓名与班级名单完全一致 |
| 登录提示“该学号不在班级名单” | 先把学号加入 roster（`scripts/import_roster.py`） |
| 上传页没有发布表单 | 该账号不是班委/课代表/管理员 |
| 网页 502 | `systemctl status huiyingzhai`、`journalctl -u huiyingzhai -n 100` |
| `/deploy/` 502 | `systemctl status huiyingzhai-panel`，确认监听 3999 |
| 附件 413 | 单个 ≤20MB；Nginx `client_max_body_size` ≥25m |
| 通知机器人没收到 | 检查 `.env`、重启后端、看 `journalctl` 中的「通知失败」 |
| QQ 主动消息失败 | `40034105` 群主未开启主动消息；`40034101` 机器人不在群 |
| 页面编辑找不到工具条 | 先用管理员姓名 + 学号登录 `/upload/` |
