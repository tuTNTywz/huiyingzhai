# 汇英斋自建后端（Ubuntu / Python）

小程序原本使用微信云开发（云函数 + 云数据库 + 云存储）。本目录用 **Python 3 + FastAPI + SQLite** 实现了同等能力，可部署在任意 Ubuntu 云服务器上：

- `wx.login` code 换取 openid，自建 token 会话
- 全部读写操作走 `/api/action`，服务端校验班委 / 管理员 / 课代表权限
- 附件上传到服务器本地 `uploads/` 目录，由 Nginx 直接提供
- 站内通知 + 微信订阅消息推送（服务端获取 access_token）

> 小程序端仍是 JavaScript（微信小程序只能使用 JS），`server/` 内已无任何 Node 代码。
> 数据库结构与原 Node 版完全一致，旧 `data/huiyingzhai.sqlite` 可直接沿用。

## 目录结构

```
server/
├── app/
│   ├── main.py      # FastAPI 入口与路由
│   ├── __main__.py  # python -m app 启动入口
│   ├── api.py       # 业务 action（读写接口）
│   ├── auth.py      # 微信登录 / token 校验
│   ├── db.py        # SQLite 文档存储
│   ├── files.py     # 附件上传 / 删除
│   ├── push.py      # 订阅消息推送
│   └── config.py    # 环境变量
├── scripts/import_roster.py  # 名单导入
├── requirements.txt
├── deploy/          # systemd / Nginx / 安装脚本
├── data/            # SQLite 数据库（运行时生成，勿提交）
├── uploads/         # 附件（运行时生成，勿提交）
└── .env.example
```

## 一、微信公众平台配置

1. **服务器域名**：开发管理 → 开发设置 → 服务器域名，把后端域名分别填入：
   - request 合法域名
   - uploadFile 合法域名
   - downloadFile 合法域名
2. 域名必须是 **HTTPS**，且已完成 ICP 备案（国内主体小程序强制要求）。
3. 记录 **AppID / AppSecret**，填入服务器 `.env`。
4. **IP 白名单**：若公众平台开启了「获取 access_token 的 IP 白名单」，需把云服务器公网 IP 加进去，否则订阅消息推送会失败。
5. **订阅消息**：申请「新作业提醒」模板，模板 ID 同时填入：
   - 服务器 `.env` 的 `WX_TMPL_HOMEWORK`
   - 小程序 `miniprogram/config.js` 的 `HOMEWORK_TMPL_ID`

## 二、本地调试

需要 Python 3.10+（Ubuntu 22.04 自带 3.10）。

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# 在 .env 中设置 DEV_MODE=true（无需微信凭据，登录固定使用 DEV_OPENID）
python scripts/import_roster.py ../roster_import.json
python -m app
```

自检：

```bash
curl http://127.0.0.1:3000/api/health
curl -X POST http://127.0.0.1:3000/api/login -H 'Content-Type: application/json' -d '{"code":"dev"}'
```

开发模式下小程序仍会调用 `wx.login`，但服务端不发请求，直接使用 `DEV_OPENID`，同一台机器上所有操作都属于同一个测试账号。

## 三、Ubuntu 生产部署

推荐 Ubuntu 22.04 / 24.04，已备案并解析到服务器的域名（下文以 `api.example.com` 为例）。

### 1. 一键初始化

在仓库根目录执行：

```bash
sudo bash server/deploy/install.sh
```

脚本会：安装 Python / Nginx / sqlite3 → 创建 `huiyingzhai` 服务用户 → 复制代码到 `/opt/huiyingzhai/server` → 创建 `.venv` 并安装依赖 → 导入名单 → 注册 systemd 服务。

> 从旧 Node 版升级时直接重跑脚本即可：旧 `src/*.js`、`package*.json` 会被 rsync 删除，`.env`、`data/`、`uploads/` 保留，SQLite 数据无需迁移。

### 2. 填写环境变量

```bash
sudo nano /opt/huiyingzhai/server/.env
```

必填：

```ini
BASE_URL=https://api.example.com
WX_APPID=wx...
WX_SECRET=...
WX_PUSH_STATE=trial   # 正式版改为 formal
```

保存后重启：

```bash
sudo systemctl restart huiyingzhai
sudo systemctl status huiyingzhai --no-pager
curl http://127.0.0.1:3000/api/health
```

### 3. 配置 Nginx 与 HTTPS

```bash
sudo cp /opt/huiyingzhai/server/deploy/nginx.conf /etc/nginx/sites-available/huiyingzhai
sudo nano /etc/nginx/sites-available/huiyingzhai     # 把 api.example.com 换成你的域名
sudo ln -s /etc/nginx/sites-available/huiyingzhai /etc/nginx/sites-enabled/huiyingzhai
sudo nginx -t && sudo systemctl reload nginx

sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d api.example.com
```

`certbot` 会自动配置 80/443 跳转与证书续期。完成后访问 `https://api.example.com/api/health` 应返回 `{"ok":true,...}`。

> 若把 `DATA_DIR` / `UPLOAD_DIR` 改到 `/var/lib/huiyingzhai`，记得同步修改 Nginx 的 `alias` 和 systemd 的读写权限（当前模板默认使用 `/opt/huiyingzhai/server` 下的目录）。

### 4. 小程序端配置

编辑 `miniprogram/config.js`：

```js
module.exports = {
  BASE_URL: "https://api.example.com",
  HOMEWORK_TMPL_ID: "6avlHCT0IR-fd1hk3c15hQDn0X0VWmuQs0rcdbN5FgM",
};
```

然后在微信开发者工具中重新编译、上传体验版 / 正式版。

## 四、日常运维

### 更新代码

```bash
cd /opt/huiyingzhai/server
# 或在仓库根目录重新执行 sudo bash server/deploy/install.sh
sudo -u huiyingzhai .venv/bin/pip install -r requirements.txt
sudo systemctl restart huiyingzhai
```

也可以使用下面的网页一键部署。

### 个人主页与网页操作

浏览器打开 `https://<后端域名>/` 是个人主页（标题 `tuTNT's home`），主页显示三个按钮：

- **进入汇英斋** → `/huiyingzhai/`（小程序内容的网页版，见下）
- **汇英斋项目介绍** → `/intro/`
- **汇英斋使用说明书** → `/manual/`

**Deploy 和「我的」不在网页显示**；部署与账号控制统一在微信小程序「我的」页面。`/deploy/`、`/me/` 即使直接访问也只显示提示。

### 小程序网页版 `/huiyingzhai/`

网页版复刻了小程序的主要浏览功能，使用同一套 **姓名 + 学号** 登录：

| 栏目 | 网页功能 |
| --- | --- |
| 作业 | 班级作业列表/详情/附件；9 个学科板块 |
| 学科 | 课程设置、课代表、任务通知、附件 |
| 活动 | 活动列表/详情 |
| 交流 | 帖子列表/详情、发帖、回复、附件（所有已登记同学可用） |
| 通知 | 站内通知列表，点击跳到对应作业/学科 |
| 我的 | 身份资料、上传入口、项目介绍/说明书、退出 |

- 发布作业/活动/学科任务仍走 `/upload/`（按职务限制），网页版只提供浏览与交流发帖/回复；
- 附件链接指向 `/uploads/...`，为公开静态文件，拿到链接即可访问，请勿上传隐私材料；
- 部署控制台仍只在管理员的小程序「我的」中；
- 页面文件：`server/deploy/huiyingzhai_web.html`，由面板直接读取。修改后需重新 `install.sh`（或手动复制到 `/usr/local/lib/huiyingzhai/`），无需重启后端。

**统一登录（姓名 + 学号）**

已删除 `tutnt`、`zhl` 两个网页账号，不再有独立网页密码。网页上传使用小程序用户表里的 **姓名 + 学号** 匹配身份；发布权限仍以小程序用户角色和名单为准。

- 小程序「我的」页面顶部点「汇英斋数据上传」，会**复制上传链接**；
- 在浏览器打开该链接（`/upload/`），填写小程序登记的姓名 + 学号；
- 按职务显示可发布内容：

| 身份 / 职务 | 可发布 |
| --- | --- |
| 管理员 | 作业、活动、学科任务（全部科目）；小程序「部署控制台」 |
| 学习委员 | 作业、活动、本人负责科目的学科任务 |
| 其他班委 | 活动、本人负责科目的学科任务（如名单里指定了科目） |
| 课代表 | 本人负责科目的学科任务 |
| 普通同学 | 不可上传 |

- 附件最多 5 个、单个 ≤20MB，发布后小程序立即可见，通知/推送同样生效。

小程序不再使用 web-view，因此**不需要配置业务域名**；小程序内的部署控制台用 `wx.request` 访问 `https://tutnt.com/me/api/...`，使用现有「服务器域名」即可。

**小程序控制页面（我的）**

- 班委 / 课代表：显示「汇英斋数据上传」（复制网页上传链接）；
- 管理员：额外显示「部署控制台」，可在小程序内直接部署最新代码并查看状态/日志；
- 「我的」页同时保留账号资料、登记/修改资料。

网页 `/deploy/` 和 `/me/` 只显示「已合并到小程序我的控制页面」提示；部署统一在管理员的小程序「部署控制台」完成。

**页面编辑（管理员）**

1. 在浏览器打开 `/upload/`，用管理员姓名 + 学号登录；
2. 打开 `/intro/` 或 `/manual/`，右下角出现「页面编辑」工具条；
3. 下载当前页面 HTML，修改后上传，页面立即更新；
4. 页面保存在 `/var/lib/huiyingzhai-panel/pages/`，删除对应文件恢复内置页；单个 ≤2MB，UTF-8 完整 HTML。

**共享密钥**

网页姓名+学号登录仍会通过共享密钥向小程序后端换取该学号的会话（只用于服务端校验，不下发 token 给页面）。`install.sh` 自动生成：

- 小程序 `/opt/huiyingzhai/server/.env` 的 `WEB_PANEL_SECRET`；
- 面板 `/etc/huiyingzhai/panel.env` 的 `PANEL_APP_SECRET`。

两端必须一致；修改后重启 `huiyingzhai` 与 `huiyingzhai-panel`。

### 微信机器人通知方案

小程序内已有「订阅消息」，但需要每位同学逐次授权。若希望**发布作业/活动/学科任务后全班自动收到一条微信消息**，推荐 **PushPlus 群组**：同学只需扫一次二维码关注公众号并加入群组，**不用安装企业微信，也不用每人绑定 token**。

#### 方案 A（推荐）：PushPlus 群组

原理：管理员在 PushPlus 建一个「群组」，同学扫码关注 PushPlus 公众号并加入；后端把消息发到群组编码（topic），所有成员在微信里收到。班级约 40 人，免费额度足够。

1. 管理员用微信登录 <https://www.pushplus.plus/>，按提示完成实名认证；
2. 「发送消息 → 一对多消息 → 新建群组」，创建班级群组；
3. 记下 **token**（个人中心/接口文档页）和 **群组编码（topic）**，把群组二维码发到班级群，同学扫码关注并加入；
4. 在服务器 `.env` 中填写（权限 600，不要提交到仓库）：

   ```bash
   sudo nano /opt/huiyingzhai/server/.env
   # 追加：
   PUSHPLUS_TOKEN=你的token
   PUSHPLUS_TOPIC=群组编码
   sudo systemctl restart huiyingzhai
   ```

5. 之后在小程序或 `/upload/` 发布作业、活动、学科任务，群组成员会收到通知（含标题、发布人、截止时间、网页链接）。

说明：

- 免费版：每个群组最多 **100 人**、每天 **200 次**请求，班级用量足够；超限当天停止发送，次日恢复；
- 消息由 PushPlus 公众号代发，内容会经过第三方服务器；不要在里面放学号、身份证等敏感信息（当前消息只含标题、发布人、截止时间）；
- token 与群组编码只放服务器 `.env`，泄露后可在 PushPlus 后台重置；
- 未配置 `PUSHPLUS_TOKEN`/`PUSHPLUS_TOPIC` 时完全静默；
- 代码位置：`server/app/robot.py`，发布逻辑在 `server/app/api.py`。

#### 方案 B：企业微信群机器人（需全员企业微信）

企业微信**外部群不支持群机器人**，只有企业内部群可以添加。也就是说，全班同学都必须加入企业微信组织，才能进这个群收到通知——如果大家不想装企业微信，不要选这个方案。

配置：企业微信群 → 右上角「…」→ 群机器人 → 添加 → 复制 Webhook，写入 `.env`：

```bash
WECHAT_ROBOT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...
```

- Webhook 等同于「向该群发消息」的权限，只放服务器 `.env`；泄露后在群机器人设置里重置；
- 限频约 **20 条/分钟**，当前只支持一个群；
- 未配置时静默，不影响 PushPlus、订阅消息和站内通知。

#### 其他服务的取舍

| 服务 | 是否适合 | 原因 |
| --- | --- | --- |
| PushPlus 群组 | ✅ 推荐 | 扫码关注即可，无需装 App，免费额度够用 |
| 企业微信机器人 | ⚠️ 仅限全员企业微信 | 外部群不支持机器人 |
| WxPusher | ❌ 不再简单 | 2025 起收消息需安装 WxPusher App，公众号不再推送 |
| Server酱 | ❌ 不适合班级 | 主要是一对一，免费额度少 |
| 个人微信机器人（itchat/Wechaty） | ❌ 不要用 | 违反微信规范、易封号、协议不稳定 |

#### 与现有推送的关系

| 渠道 | 是否需授权 | 覆盖范围 | 建议 |
| --- | --- | --- | --- |
| 小程序订阅消息 | 每人逐次授权 | 已授权的小程序用户 | 保留 |
| 站内通知（小程序/网页） | 否 | 所有登录用户 | 保留 |
| PushPlus 群组 | 扫码加入一次 | 群组成员 | 推荐新增 |
| 企业微信群机器人 | 进内部群 | 企业微信成员 | 备选 |

在**小程序「我的」→ 部署控制台**点击部署后：

1. 从 GitHub 下载当前分支的源码压缩包（默认 `tuTNTywz/huiyingzhai` 的 `main`）；
2. 校验代码包结构，备份当前版本（保留最近 5 份）；
3. 把压缩包中的 `server/` 同步到 `/opt/huiyingzhai/server`；
4. **保留** `.env`、`data/`、`uploads/`、`.venv`；
5. `requirements.txt` 变化时执行 `pip install`（systemd 单元不随部署更新，改动后需重跑 `install.sh`）；
6. 重启 `huiyingzhai`，轮询 `/api/health`；失败自动回滚并再次重启。

> 这里的自动备份只保留**代码**（不含 `.env`、`data/`、`uploads/`）；数据库与附件仍需按本节末尾「备份」定期备份。

小程序部署页会显示当前提交、上次结果和实时日志。部署脚本与日志：

```bash
sudo /usr/local/sbin/huiyingzhai-deploy        # 手动执行一次（等价于点按钮）
sudo tail -f /var/log/huiyingzhai-deploy.log
journalctl -u huiyingzhai-deploy -n 50 --no-pager
```

**权限模型**（为什么不把部署直接写进 FastAPI）：

- 个人主页/部署面板 `huiyingzhai-panel` 以独立用户运行，只监听 `127.0.0.1:3999`；账号会话为 12 小时 HttpOnly Cookie，所有写操作还要求 CSRF Token；
- 面板只能往 `/var/lib/huiyingzhai-panel/` 写一个请求文件，`huiyingzhai-deploy.path` 监听到后触发 root 的 `huiyingzhai-deploy.service` 执行 `/usr/local/sbin/huiyingzhai-deploy`；
- FastAPI 服务本身仍然是无特权用户 + `NoNewPrivileges=true`，即使应用被攻破也不能直接提权重启服务；
- 部署脚本、面板代码和 systemd 单元都由 root 安装，**不会**从 GitHub 自动更新；修改 `server/deploy/` 后需要重新执行 `sudo bash server/deploy/install.sh` 才生效。

**安装与 Nginx**

`install.sh` 会自动创建面板用户、配置、systemd 单元并启动。

新服务器可以直接把 `deploy/nginx.conf` 作为站点配置（已包含限速 zone 和 location）。已有站点建议用独立文件，避免改动 certbot 生成的配置：

```nginx
# 1) /etc/nginx/conf.d/huiyingzhai-panel-zone.conf（http 上下文）
limit_req_zone $binary_remote_addr zone=huiyingzhai_deploy:10m rate=120r/m;

# 2) /etc/nginx/snippets/huiyingzhai-panel.conf
location = / {
    proxy_pass http://127.0.0.1:3999;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 30s;
}

location = /deploy {
    return 301 /deploy/;
}
location /deploy/ {
    # allow 203.0.113.10;   # 建议填写自己的固定出口 IP
    # deny all;
    limit_req zone=huiyingzhai_deploy burst=40 nodelay;
    proxy_pass http://127.0.0.1:3999;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 30s;
}

location = /intro { return 301 /intro/; }
location /intro/ {
    proxy_pass http://127.0.0.1:3999;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 30s;
}

location = /manual { return 301 /manual/; }
location /manual/ {
    proxy_pass http://127.0.0.1:3999;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 30s;
}

location = /me { return 301 /me/; }
location /me/ {
    limit_req zone=huiyingzhai_deploy burst=40 nodelay;
    proxy_pass http://127.0.0.1:3999;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 30s;
}

location = /upload { return 301 /upload/; }
location /upload/ {
    limit_req zone=huiyingzhai_deploy burst=40 nodelay;
    proxy_pass http://127.0.0.1:3999;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 30s;
}

location = /huiyingzhai { return 301 /huiyingzhai/; }
location /huiyingzhai/ {
    limit_req zone=huiyingzhai_deploy burst=40 nodelay;
    proxy_pass http://127.0.0.1:3999;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
    client_max_body_size 25m;
}

# 3) 在 HTTPS server { } 内加一行
include /etc/nginx/snippets/huiyingzhai-panel.conf;
```

> `limit_req_zone` 只能定义一次；如果站点文件里已有，就不要重复添加。
> `sudo nginx -t && sudo systemctl reload nginx`。

**配置**

```bash
sudo nano /etc/huiyingzhai/deploy.conf   # 仓库、分支、私有仓库 token、GitHub 镜像
sudo nano /etc/huiyingzhai/panel.env     # 面板端口、小程序共享密钥、状态路径
```

- 私有仓库：在 `deploy.conf` 中设置 `GITHUB_TOKEN`（`Contents` 只读即可）。
- 服务器直连 GitHub 慢：设置 `DEPLOY_ARCHIVE_URL_TEMPLATE`，例如
  `https://ghproxy.example.com/https://github.com/%REPO%/archive/refs/heads/%BRANCH%.tar.gz`。
- 网页上传用小程序用户表里的姓名 + 学号匹配，不再有独立网页账号/密码。
- 部署控制仅限小程序用户角色为管理员（管理员/班长）的账号。
- 请使用 HTTPS，并尽量再加 Nginx IP 白名单。

**故障排查**

| 现象 | 处理 |
| --- | --- |
| `/` 404 | Nginx 未加 `location = /` 反代，或未 reload |
| `/intro/` 或 `/manual/` 404 | Nginx 未加对应 location，或未 reload |
| `/me/` 404 | Nginx 未加 `/me/` location，或未 reload |
| `/upload/` 404 | Nginx 未加 `/upload/` location，或未 reload |
| `/huiyingzhai/` 404 | Nginx 未加 `/huiyingzhai/` location，或未 reload |
| `/huiyingzhai/` 显示文件缺失 | 重新执行 `install.sh`，或把 `server/deploy/huiyingzhai_web.html` 复制到 `/usr/local/lib/huiyingzhai/` |
| 网页版登录后空白 | 先在 `/upload/` 验证同一姓名+学号；F12 看 `/me/api/session` 是否 401 |
| 网页发帖/回复失败 | 需在小程序完成登记；附件单个 ≤20MB、最多 5 个 |
| PushPlus 没收到消息 | 检查 `.env` 的 `PUSHPLUS_TOKEN`/`PUSHPLUS_TOPIC`、`systemctl restart huiyingzhai`；看 `journalctl -u huiyingzhai` 的「PushPlus 通知失败」；确认同学已加入群组 |
| 企业微信没收到消息 | 检查 `WECHAT_ROBOT_WEBHOOK`；确认是**内部群**（外部群不支持机器人） |
| 上传页登录失败 | 填写小程序登记的姓名 + 学号；未登记或姓名不匹配会拒绝 |
| 小程序「部署控制台」打不开 | 仅管理员可见；确认小程序「服务器域名」包含 `https://tutnt.com` |
| 上传页没有发布表单 | 绑定学号不是班委/课代表，或未在小程序完成登记 |
| 共享密钥不匹配 | `WEB_PANEL_SECRET` 与 `PANEL_APP_SECRET` 必须一致，修改后重启两个服务 |
| 附件上传 413 / 超过 20MB | 单个附件上限 20MB；Nginx `client_max_body_size` 需 ≥ 25m |
| `/deploy/` 404 | Nginx 未加 `/deploy/` 反代，或未 reload |
| `/deploy/` 502 | `systemctl status huiyingzhai-panel`，看端口是否为 3999 |
| 部署页提示「请先登录」 | 正常，先到 `/me/` 登录；登录后会回到部署页 |
| 提示姓名与学号不匹配 | 确认与小程序「我的」登记信息一致；只有已登记用户可登录 |
| 登录提示尝试过于频繁 | 5 分钟内失败次数过多，等待几分钟再试 |
| 页面提示 401 | 会话过期或面板重启，重新登录即可 |
| 上传提示 413 / 超过 2MB | 页面 HTML 单文件上限 2MB；Nginx `client_max_body_size` 需 ≥ 25m |
| 上传后页面没变化 | 确认上传的是完整 `.html` 文件；刷新页面；检查 `/var/lib/huiyingzhai-panel/pages/` 权限 |
| 看不到「页面编辑」工具条 | 先在 `/upload/` 用管理员姓名 + 学号登录，再打开 `/intro/` 或 `/manual/` |
| 提示已有任务 | 等待当前部署结束；超过 15 分钟可 `sudo systemctl stop huiyingzhai-deploy` 后再试 |
| 点了没反应 | `systemctl status huiyingzhai-deploy.path`、`journalctl -u huiyingzhai-deploy` |
| 部署失败已回滚 | 看日志中的具体步骤；修复后重新部署，备份在 `/opt/huiyingzhai/backups/` |
| GitHub 下载失败 | 检查服务器出网；在 `deploy.conf` 配置镜像模板 |

### 日志

```bash
sudo journalctl -u huiyingzhai -f
sudo tail -f /var/log/nginx/access.log
```

### 备份

需要备份三处：

```bash
# 数据库（WAL 模式，建议先停服务或用 sqlite3 .backup）
sudo systemctl stop huiyingzhai
sudo sqlite3 /opt/huiyingzhai/server/data/huiyingzhai.sqlite ".backup '/root/huiyingzhai-$(date +%F).sqlite'"
sudo systemctl start huiyingzhai

# 附件
sudo tar czf /root/uploads-$(date +%F).tar.gz -C /opt/huiyingzhai/server uploads

# 项目介绍/使用说明书的自定义页面
sudo tar czf /root/huiyingzhai-panel-$(date +%F).tar.gz -C /var/lib/huiyingzhai-panel .
```

恢复时把数据库文件与 `uploads/` 放回原位置并 `chown -R huiyingzhai:huiyingzhai`；面板目录恢复后执行 `chown -R huiyingzhai-panel:huiyingzhai-panel /var/lib/huiyingzhai-panel`。

### 新增 / 修改名单

```bash
cd /opt/huiyingzhai/server
sudo -u huiyingzhai .venv/bin/python scripts/import_roster.py /path/to/roster.json
```

脚本按学号 upsert，可重复执行。JSON 数组与 JSON Lines 都支持。

## 五、接口说明

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/login` | `{code}` → `{token, user, registered}` |
| POST | `/api/action` | 需 `Authorization: Bearer <token>`，body `{action, data}` |
| POST | `/api/upload` | 需登录，multipart 字段 `file`，可选 `dir` |
| POST | `/api/deleteFiles` | 需登录，body `{fileList: [...]}` |
| GET | `/api/health` | 健康检查 |
| GET | `/uploads/...` | 附件静态访问 |

业务 action 与改造前云函数保持一致，另新增读接口：
`listHomeworks`、`getHomework`、`listActivities`、`getActivity`、`listPosts`、`getPost`、`listReplies`、`getSubjectSettings`、`getSubjectRep`、`listSubjectTasks`、`lookupRoster`。

## 六、常见问题

**小程序提示「不在以下 request 合法域名列表中」**
公众平台服务器域名未配置或使用了 HTTP；开发阶段可在开发者工具「详情 → 本地设置」勾选「不校验合法域名」。

**登录一直失败**
检查 `.env` 的 AppID/Secret 是否成对填写；开发阶段可设 `DEV_MODE=true` 绕过微信接口。生产环境需确保服务器能访问 `api.weixin.qq.com`。

**推送收不到**
确认模板 ID 两端一致、用户已在小程序内开启提醒、`WX_PUSH_STATE` 与实际版本一致、服务器 IP 在公众平台白名单内。站内通知不受影响。

**上传 413 / 附件太大**
Nginx `client_max_body_size`（当前 25m）与 `.env` 的 `MAX_UPLOAD_MB`（默认 20）都要满足。

**`python3 -m venv` 报错**
安装 `sudo apt-get install -y python3-venv`；若 pip 下载慢可加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。

**`ModuleNotFoundError: app`**
需在 `server/` 目录下执行 `python -m app`（systemd 已设置 `WorkingDirectory`）。

**名单导入提示「名单文件不存在」**
导入以 `huiyingzhai` 用户身份执行，而仓库若放在 `/home/ubuntu` 下，家目录权限可能让它读不到该文件。`install.sh` 会先把名单复制到 `/opt/huiyingzhai/server/roster_import.json` 再导入；手动导入时也请先复制到 `/tmp` 或服务目录并 `chmod 644`。

**旧的 Node 文件还在**
重跑 `install.sh` 会通过 `rsync --delete` 清理；也可手动删除 `src/`、`package.json`、`package-lock.json`、`node_modules/`。
