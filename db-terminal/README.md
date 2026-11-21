# 汇英斋数据库终端（dbterm）

一个直接读写小程序服务端 SQLite 数据库的命令行终端，用来查名单、改职务、删帖子、修作业截止时间、备份和导入导出数据。

- **Windows**：通过 SSH 连接服务器，在服务器上运行终端，适合在本地电脑管理线上数据库
  - 图形界面：`dbterm_gui.bat`（推荐）
  - 命令行终端：`dbterm.bat`
- **Linux / macOS**：仍然在服务器本机直接操作数据库，用法与之前完全一致
- 两端共用同一个 `dbterm.py`，Windows 只是多了一层 SSH 连接和自动上传
- 纯 Python 标准库，不需要安装任何依赖（Python 3.8+）

> 数据库文件默认是服务端的 `server/data/huiyingzhai.sqlite`，表结构与 `server/app/db.py` 完全一致。修改立即生效，不需要重启服务。

## 目录内容

```
db-terminal/
├── dbterm.py             # 终端本体（两端共用；Linux 本地直接运行）
├── dbterm.bat            # Windows 命令行启动器（远程 SSH）
├── dbterm_remote.py      # Windows 远程连接器
├── dbterm_gui.py         # Windows 图形界面
├── dbterm_gui.bat        # Windows 图形界面启动器
├── remote.env.example    # Windows 远程配置模板
├── dbterm.sh             # Linux / macOS 启动器（本地，保持不变）
├── README.md
├── backups/              # Linux 本地运行时生成
└── exports/
```

---

## 一、Windows：远程访问服务器

### 1. 准备

1. Windows 10/11 启用 **OpenSSH 客户端**：设置 → 应用 → 可选功能 → 添加功能 → OpenSSH 客户端。
2. 安装 Python 3.8+（Windows 官方安装包自带 tkinter），安装时勾选 **Add python.exe to PATH**。
3. 确认你能 SSH 登录服务器：`ssh root@你的服务器IP`。
4. **强烈建议配置 SSH 公钥免密登录**，否则每次启动可能输入两次密码（scp 和 ssh 各一次）：

   ```bat
   ssh-keygen -t ed25519
   type %USERPROFILE%\.ssh\id_ed25519.pub | ssh root@你的服务器IP "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
   ```

### 2. 配置 remote.env

首次运行 `dbterm_gui.bat` 或 `dbterm.bat` 会自动生成 `remote.env` 并用记事本打开。至少填写：

```ini
HOST=你的服务器IP或域名
USER=root
PORT=22
SERVER_ROOT=/opt/huiyingzhai/server
SUDO_USER=huiyingzhai
```

其余配置见 `remote.env.example`，常用项：

| 配置 | 说明 |
| --- | --- |
| `HOST` | 服务器 IP、域名，或 `~/.ssh/config` 中的 Host 别名 |
| `USER` | SSH 登录用户，通常是 `root` 或 `ubuntu` |
| `PORT` | SSH 端口，默认 22 |
| `SSH_KEY` | 私钥路径，如 `C:\Users\你\.ssh\id_ed25519`；图形界面必须使用密钥登录 |
| `SSH_OPTIONS` | 额外 ssh 参数，如 `-o StrictHostKeyChecking=accept-new` |
| `SERVER_ROOT` | 服务端目录，`install.sh` 默认 `/opt/huiyingzhai/server` |
| `SUDO_USER` | 以哪个用户执行，默认 `huiyingzhai`；留空则直接用 SSH 用户 |
| `PYTHON` | 服务器上的解释器，默认 `python3` |
| `REMOTE_SCRIPT` | `dbterm.py` 上传位置，默认 `/tmp/huiyingzhai-dbterm.py` |
| `REMOTE_HOME` | 远程备份/导出目录，默认 `/opt/huiyingzhai/.dbterm` |

`remote.env` 含服务器地址，已被 `.gitignore` 忽略。

### 3. 图形界面（推荐）

双击 `dbterm_gui.bat`（或执行 `python dbterm_gui.py`）打开管理界面：

- 左侧集合列表，中间可搜索的文档表格，右侧 JSON 编辑器
- 工具栏：刷新、新增、保存、删除、执行 SQL、备份、状态
- 快捷键：`Ctrl+S` 保存、`Ctrl+N` 新增、`Ctrl+R` / `F5` 刷新、`Ctrl+E` 执行 SQL、`Ctrl+F` 搜索
- 文档表格支持右键「复制 ID」「删除文档」，选中即加载到编辑器
- 编辑 JSON 后保存会按 `_id` 整份写回；新增文档会自动生成 `_id` 和常用字段模板
- 「备份」会自动执行远程备份，并询问是否下载到本地
- 未经保存的编辑在切换文档 / 集合时会提示，避免误丢内容

> 图形界面通过 `BatchMode` 连接 SSH，**不能输入 SSH 密码**，请先配置密钥登录（可用 ssh-agent）。
> 首次连接请先在 cmd 里执行一次 `ssh 用户@服务器` 接受主机指纹，或在 `remote.env` 设置
> `SSH_OPTIONS=-o StrictHostKeyChecking=accept-new`。
> `SUDO_USER=huiyingzhai` 需要免密 sudo（SSH 用户为 root 时通常没问题）。
> 启动失败时详情会写入 `dbterm_gui.log`。

### 4. 命令行终端

```bat
dbterm.bat                     :: 进入远程交互终端
dbterm.bat --check             :: 自检：SSH、sudo、数据库、路径
dbterm.bat status              :: 单次执行
dbterm.bat find roster position=班长
dbterm.bat help                :: 查看服务器端完整命令帮助
```

运行过程：

1. 本地读取 `remote.env`
2. 把 `dbterm.py` 上传到服务器的 `REMOTE_SCRIPT`（内容未变时自动跳过）
3. 通过 `ssh -t` 以 `SUDO_USER` 身份启动远程终端

因此 **服务器上不需要安装或部署 db-terminal**，只要能用 SSH 登录即可。Linux 版本的 `dbterm.py` / `dbterm.sh` 完全不变。

> 如果服务器重启导致 `/tmp` 被清空，终端会自动检测并重新上传。
> `SUDO_USER` 需要 sudo 权限；首次可能提示输入 sudo 密码。如果 SSH 用户是 `root`，通常不会提示。

### 5. 远程备份位置

远程执行 `backup`、`export` 时，默认写入：

- 备份：`/opt/huiyingzhai/.dbterm/backups/`
- 导出：`/opt/huiyingzhai/.dbterm/exports/`

需要下载到 Windows 时：

```bat
scp root@你的服务器IP:/opt/huiyingzhai/.dbterm/backups/huiyingzhai-20260101-120000.sqlite .
```

也可以在执行时直接指定远程路径，例如 `dbterm.bat backup /opt/huiyingzhai/backups/manual.sqlite`。

---

## 二、Linux / macOS：本机直接操作（保持不变）

在服务器上或数据库所在机器上运行：

```bash
python3 dbterm.py                              # 当前目录
python3 dbterm.py --server-root /opt/huiyingzhai/server
sudo -u huiyingzhai python3 dbterm.py --server-root /opt/huiyingzhai/server
```

或使用启动脚本：

```bash
./dbterm.sh
```

数据库路径确定顺序：

1. 命令行 `--db <路径>`
2. `server/.env` 中的 `DATA_DIR`（相对路径按 `server/` 解析）
3. 默认 `server/data/huiyingzhai.sqlite`

附件目录：`--upload-dir` → `.env` 的 `UPLOAD_DIR` → `server/uploads`。

Linux 下 `dbterm.sh` 和本地交互方式与之前完全一致，`DBTERM_HOME` 未设置时备份/导出仍写在 `db-terminal/backups`、`db-terminal/exports`。

---

## 三、命令一览（两端通用）

Windows 把所有参数原样传给服务器上的 `dbterm.py`，所以下面所有命令在 Windows 上写成 `dbterm.bat ...` 即可。图形界面里的操作本质上也是调用这些命令。

### 查询

| 命令 | 说明 |
| --- | --- |
| `status` | 数据库路径、大小、集合数量、附件目录 |
| `collections` | 列出所有集合及文档数 |
| `tables` | 列出 SQLite 表（`documents` / `sessions`）及行数 |
| `ls <集合> [数量]` | 浏览集合，默认按 `createTime` 倒序取 20 条 |
| `show <集合> <ID>` | 查看完整 JSON，ID 可只写前几位 |
| `find <集合> [条件...]` | 条件查询 |

查询选项：

```
--limit N / -n N   限制条数（0 或 --all 表示不限）
--sort 字段        指定排序字段
--asc / --desc     升序 / 降序（默认降序）
--json             输出完整 JSON（ID 不会截断）
```

条件支持 `=`、`!=`、`~=`（包含）、`>`、`<`、`>=`、`<=`，字段可用点号访问嵌套值：

```bash
find roster position=班长
find users ~className=计算机          # 班级名包含“计算机”
find users registered=true
find homeworks deadline>1760000000000
find posts ~content=图书馆
find subject_tasks subject=math --limit 5 --sort createTime --asc
```

### 修改

| 命令 | 说明 |
| --- | --- |
| `set <集合> <ID> 字段=值 ...` | 修改一个或多个字段 |
| `unset <集合> <ID> 字段 ...` | 删除字段 |
| `insert <集合> '<JSON>'` | 插入新文档 |
| `put <集合> <ID> <base64 JSON>` | 整份保存文档（图形界面内部使用） |
| `rm <集合> <ID> [--with-files]` | 删除文档 |
| `rmwhere <集合> 条件... [--with-files]` | 按条件批量删除 |
| `sql "<SQL>"` | 执行任意 SQL，写操作需确认 |

示例：

```bash
# 把某个用户改成管理员、职务改为班长
set users 1a2b3c4d role=admin position=班长

# 修改嵌套字段（第 1 个附件的类型）
set posts 9f8e7d6c attachments.0.type=image

# 新增字段（新字段会按 JSON 解析；要强制字符串用 s: 前缀）
set users 1a2b3c4d remark=s:重点关注

# 删除字段
unset users 1a2b3c4d remark

# 插入一条名单
insert roster '{"studentId":"20240001","name":"张三","position":"普通同学","repSubjects":[]}'

# 删除一条帖子并清理它的附件文件
rm posts 9f8e7d6c --with-files

# 删除某科目的全部任务通知
rmwhere subject_tasks subject=math

# 直接改数据库（写 SQL 会先展示语句并要求确认）
sql "UPDATE documents SET data = json_set(data, '$.deadline', 1760000000000) WHERE collection='homeworks'"
sql "SELECT collection, COUNT(*) AS n FROM documents GROUP BY collection"
```

`set` 会按字段原来的类型转换：

- 原值是字符串（如 `studentId`、`role`）→ 按字符串写入，不会把 `00123` 变成数字
- 原值是数字 → 按整数 / 浮点数写入
- 原值是布尔 → `true` / `false`
- 原值是数组 / 对象 → 必须提供合法 JSON
- 新字段 → 能解析成 JSON 就按 JSON，否则按字符串；`s:` 前缀可强制字符串

> `_id` 不允许修改或删除。需要换 ID 请导出后重新插入。

### 备份与导入导出

```bash
backup                          # 备份到 backups/huiyingzhai-<时间>.sqlite
backup /path/before.sqlite      # 指定路径

export posts                    # 导出到 exports/posts-<时间>.json
export posts posts.json         # 指定路径

import roster roster_import.json            # 按 _id 覆盖，没有 _id 的作为新文档
import posts posts.json --replace           # 先清空 posts，再导入（危险）
```

备份使用 SQLite 官方 backup API，服务运行中也能得到一致快照。**建议每次批量修改前先 `backup`。**

### 全局选项

写在命令前面（Windows 会把它们继续传给远程 `dbterm.py`）：

```
--db PATH            指定数据库
--server-root DIR    指定 server 目录（读取 .env）
--upload-dir DIR     指定附件目录
-y, --yes            跳过删除类确认（脚本用）
-r, --readonly       只读模式，禁止任何写入
--json               查询结果输出 JSON
--no-color           关闭颜色
--version            版本
```

删除类命令也支持把 `--yes` / `-y` 写在参数末尾：

```bash
rm posts 9f8e7d6c --yes
```

Windows 启动器自身还支持：

```
--check             检查 SSH、sudo、数据库访问是否正常
--deploy            只上传最新的 dbterm.py
--no-tty            不为远程命令分配终端（脚本自动化用）
--config <文件>     使用其他 remote.env
```

Windows 示例：

```bat
dbterm.bat --no-color ls users
dbterm.bat find roster studentId=20240001
dbterm.bat set users 1a2b3c4d role=admin --yes
dbterm.bat backup
```

### 交互模式

Windows 双击 `dbterm.bat` 或执行不带参数的 `dbterm.bat`，会在服务器上打开交互终端：

```
dbterm> find roster position=班长
dbterm> set users 1a2b3c4d role=committee position=学习委员
dbterm> rm posts 9f8e7d6c --with-files
dbterm> backup
dbterm> exit
```

输入 `help` 查看命令，`collections` 查看集合。

---

## 四、常见场景

**查某个学号是谁、什么职务**

```bash
find roster studentId=20240001
show roster <ID>
```

**把某人的账号角色改掉**

`users` 文档里的 `role` 只有三种：`student`、`committee`、`admin`。

```bash
find users studentId=20240001
set users <ID> role=committee position=学习委员
```

**清理测试数据**

```bash
find posts ~content=测试
rmwhere posts ~content=测试
```

**恢复备份**

先停服务，再把备份文件复制回服务端的 `server/data/huiyingzhai.sqlite`（同时删除同目录下的 `-wal`、`-shm` 文件），最后启动服务。

---

## 五、安全提示

- 工具直接操作生产数据库。批量删除、`--replace`、写 SQL 前先 `backup`。
- 删除文档时，只有加 `--with-files` 才会删除 `uploads/` 里的物理附件；不加则只删数据库记录。
- `--readonly` 可以防止误操作，适合只想查看数据的时候。
- 修改 `users.role` / `position` 立即影响该用户的发布、删除权限，请谨慎。
- SQLite 处于 WAL 模式，服务运行中单条写入是安全的；但不要在高峰期做大批量 `rmwhere` / `import`。
- `remote.env` 含服务器地址和密钥路径，不要提交到 git（已在 `.gitignore` 中）。
- Windows 远程模式通过 SSH 传输，建议使用密钥登录；不要把密码写进配置文件。

## 六、Windows 远程排错

**图形界面打不开或闪退**
先用命令行启动查看错误：`python dbterm_gui.py`。详细信息也会写入 `dbterm_gui.log`。常见原因是 `remote.env` 未填写、Python 缺少 tkinter，或 SSH 连接失败。

**图形界面提示 SSH 密码**
图形界面无法输入密码，请在 `remote.env` 配置 `SSH_KEY`，或启动 ssh-agent 并 `ssh-add`。首次连接还需在 cmd 中手动 `ssh` 一次以接受主机指纹，或设置 `SSH_OPTIONS=-o StrictHostKeyChecking=accept-new`。

**提示未找到 ssh / OpenSSH 客户端**
设置 → 应用 → 可选功能 → 添加功能 → 勾选「OpenSSH 客户端」，或执行：

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
```

**SSH 连接失败（返回码 255）**
先在 cmd 里直接执行 `ssh 你的用户@你的服务器`，确认能登录；再检查 `HOST`、`USER`、`PORT`、`SSH_KEY`。

**`Permission denied` / `sudo: a password is required`**
`SUDO_USER=huiyingzhai` 需要当前 SSH 用户有 sudo 权限。可改用 root 登录，或给对应用户配置免密 sudo。

**数据库不存在 / 路径不对**
先在服务器确认：

```bash
ls -l /opt/huiyingzhai/server/data/
cat /opt/huiyingzhai/server/.env | grep -E 'DATA_DIR|UPLOAD_DIR'
```

如果不使用默认目录，修改 `remote.env` 中的 `SERVER_ROOT`，或让 `dbterm.py` 通过 `--db` 指向实际路径（Windows 下写在命令里）。

**上传失败**
检查 `REMOTE_SCRIPT` 所在目录是否可写；也可以改为 `/tmp/huiyingzhai-dbterm.py`。版本更新后会自动重新上传，不需要手动删除缓存。

**终端没有颜色 / 出现乱码转义符**
Windows 10 旧版控制台可能不支持 ANSI，使用 Windows Terminal，或加 `--no-color`：

```bat
dbterm.bat --no-color
```

**想确认远程终端版本**
`dbterm.bat --check` 会显示数据库状态；`dbterm.bat --deploy` 会强制重新上传本地最新版本。
