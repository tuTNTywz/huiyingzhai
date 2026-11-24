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

### 个人主页与网页部署

浏览器打开 `https://<后端域名>/` 是个人主页（标题 `tuTNT's home`），页面只有一个「Deploy」按钮；点击进入 `https://<后端域名>/deploy/`，用账号登录后即可部署。

`install.sh` 首次运行会创建账号文件并打印随机密码（默认两个账号 `tutnt`、`zhl`）。密码只以 PBKDF2-HMAC-SHA256 哈希保存在 `/var/lib/huiyingzhai-panel/accounts.json`。登录后页面里有「修改密码」；忘记密码可用命令重置：

```bash
# 列出账号
sudo -u huiyingzhai-panel python3 /usr/local/lib/huiyingzhai/manage_accounts.py list

# 重置密码（从 stdin 读取，避免密码出现在 ps）
printf '%s\n' '新密码' | sudo -u huiyingzhai-panel python3 /usr/local/lib/huiyingzhai/manage_accounts.py set tutnt
```

点击「部署最新代码」后：

1. 从 GitHub 下载当前分支的源码压缩包（默认 `tuTNTywz/huiyingzhai` 的 `main`）；
2. 校验代码包结构，备份当前版本（保留最近 5 份）；
3. 把压缩包中的 `server/` 同步到 `/opt/huiyingzhai/server`；
4. **保留** `.env`、`data/`、`uploads/`、`.venv`；
5. `requirements.txt` 变化时执行 `pip install`（systemd 单元不随部署更新，改动后需重跑 `install.sh`）；
6. 重启 `huiyingzhai`，轮询 `/api/health`；失败自动回滚并再次重启。

> 这里的自动备份只保留**代码**（不含 `.env`、`data/`、`uploads/`）；数据库与附件仍需按本节末尾「备份」定期备份。

页面会显示当前部署的提交、上次部署结果和实时日志。部署脚本与日志：

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

`install.sh` 会自动创建面板用户、配置、账号、systemd 单元并启动，结束后打印初始账号密码。首次升级旧部署时，还需要给 Nginx 增加个人主页和部署面板反代。`deploy/nginx.conf` 已包含；已有站点可手动加入：

```nginx
# http 上下文（sites-enabled 文件顶部）
limit_req_zone $binary_remote_addr zone=huiyingzhai_deploy:10m rate=120r/m;

# server 块内
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
```

`sudo nginx -t && sudo systemctl reload nginx`。

**配置**

```bash
sudo nano /etc/huiyingzhai/deploy.conf   # 仓库、分支、私有仓库 token、GitHub 镜像
sudo nano /etc/huiyingzhai/panel.env     # 面板端口与账号文件路径（无密钥）
```

- 私有仓库：在 `deploy.conf` 中设置 `GITHUB_TOKEN`（`Contents` 只读即可）。
- 服务器直连 GitHub 慢：设置 `DEPLOY_ARCHIVE_URL_TEMPLATE`，例如
  `https://ghproxy.example.com/https://github.com/%REPO%/archive/refs/heads/%BRANCH%.tar.gz`。
- 账号密码只保存哈希；请使用 HTTPS，并尽量再加 Nginx IP 白名单。
- 账号文件权限 600、属主 `huiyingzhai-panel`；如需增删账号，使用 `manage_accounts.py`。

**故障排查**

| 现象 | 处理 |
| --- | --- |
| `/` 404 | Nginx 未加 `location = /` 反代，或未 reload |
| `/deploy/` 404 | Nginx 未加 `/deploy/` 反代，或未 reload |
| `/deploy/` 502 | `systemctl status huiyingzhai-panel`，看端口是否为 3999 |
| 登录提示用户名或密码不正确 | 确认账号；忘记密码用 `manage_accounts.py set` 重置 |
| 登录提示尝试过于频繁 | 5 分钟内失败次数过多，等待几分钟再试 |
| 页面提示 401 | 会话过期或面板重启，重新登录即可 |
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

需要备份两处：

```bash
# 数据库（WAL 模式，建议先停服务或用 sqlite3 .backup）
sudo systemctl stop huiyingzhai
sudo sqlite3 /opt/huiyingzhai/server/data/huiyingzhai.sqlite ".backup '/root/huiyingzhai-$(date +%F).sqlite'"
sudo systemctl start huiyingzhai

# 附件
sudo tar czf /root/uploads-$(date +%F).tar.gz -C /opt/huiyingzhai/server uploads
```

恢复时把数据库文件与 `uploads/` 放回原位置并 `chown -R huiyingzhai:huiyingzhai`。

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
