# CF Analyzer Web 部署手册（从零开始）

适用于：**只有一台 Windows 电脑、没接触过服务器** 的开发者。

---

## 第 0 步：购买云服务器（10 分钟）

### 推荐：腾讯云轻量应用服务器（香港地域）

> 选香港是因为：**不需要域名备案**，买完直接用。国内地域需要备案（2-4 周），后面再迁也行。

1. 打开 https://cloud.tencent.com/product/lighthouse
2. 点击「立即选购」
3. 配置选择：
   - **地域**：香港
   - **镜像**：Ubuntu 22.04 LTS
   - **套餐**：2核 2G 内存（最低够用）
   - 时长：按月
4. 付款完成

### 记下关键信息

购买后进入 [控制台](https://console.cloud.tencent.com/lighthouse/instance)，点击你的实例：

- **公网 IP**：比如 `43.xxx.xxx.xxx`（后面用 `你的IP` 代替）
- **重置密码**：点击「重置密码」设一个 root 密码（后面用 `你的密码` 代替）

### 开放防火墙端口

在实例详情页 → **防火墙** → 添加规则：

| 端口 | 协议 | 说明 |
|---|---|---|
| 22 | TCP | SSH 远程连接（默认已开） |
| 80 | TCP | HTTP |
| 443 | TCP | HTTPS |
| 6010 | TCP | 测试用（调通后可关闭） |

---

## 第 1 步：从 Windows 连接服务器（2 分钟）

打开 **Windows Terminal**（Win11 自带）或 **PowerShell**：

```bash
ssh root@你的IP
```

第一次连接会问 `Are you sure you want to continue connecting?`，输入 `yes` 回车。
然后输入密码（输入时看不到字符，正常的），回车。

看到类似这样的提示符就成功了：
```
root@VM-xxx:~#
```

> 如果 `ssh` 命令不可用，去 https://mobaxterm.mobatek.net/ 下载 MobaXterm，打开后点 Session → SSH → 填 IP 和 root。

---

## 第 2 步：安装 Docker（3 分钟）

以下命令全部在服务器上执行（SSH 窗口里粘贴）：

```bash
# 一键安装 Docker
curl -fsSL https://get.docker.com | sh

# 安装 docker compose 插件
apt update && apt install -y docker-compose-plugin

# 验证
docker --version
docker compose version
```

看到版本号就说明装好了。

---

## 第 3 步：安装 Nginx + 申请 SSL 证书工具（1 分钟）

```bash
apt install -y nginx certbot python3-certbot-nginx
```

---

## 第 4 步：上传代码到服务器（3 分钟）

**在你的 Windows 电脑上**（新开一个 PowerShell 窗口），执行：

```powershell
scp -r "D:\工作相关\cf_analyzer_web\*" root@你的IP:/root/cf_analyzer_web/
```

输入服务器密码，等待上传完成。

> 如果 scp 报错路径问题，也可以先把 `cf_analyzer_web` 文件夹压缩成 zip，用 `scp` 传 zip 再解压：
> ```powershell
> scp cf_analyzer_web.zip root@你的IP:/root/
> # 然后在服务器上
> cd /root && apt install -y unzip && unzip cf_analyzer_web.zip
> ```

验证（在服务器上）：

```bash
ls /root/cf_analyzer_web/
# 应该看到 app.py  auth.py  db.py  Dockerfile  docker-compose.yml 等文件
```

---

## 第 5 步：创建 GitHub OAuth App（5 分钟）

1. 浏览器打开 https://github.com/settings/developers
2. 点击 **New OAuth App**
3. 填写：

| 字段 | 值 |
|---|---|
| Application name | `CF Analyzer` |
| Homepage URL | `http://你的IP:6010` |
| Authorization callback URL | `http://你的IP:6010/auth/callback` |

> 后面如果绑了域名，回来改成 `https://你的域名`

4. 点击 **Register application**
5. 页面上可以看到 **Client ID**，复制下来
6. 点击 **Generate a new client secret**，复制 **Client Secret**（只显示一次！）

另外查一下你的 **GitHub 数字 ID**（用于管理员权限）：

```bash
# 在服务器上执行，替换 your_github_username
curl -s https://api.github.com/users/your_github_username | grep '"id"'
```

输出类似 `"id": 12345678`，记下这个数字。

---

## 第 6 步：配置环境变量（5 分钟）

```bash
cd /root/cf_analyzer_web

# 复制模板
cp .env.example .env

# 先生成两个密钥
echo "JWT_SECRET: $(openssl rand -hex 32)"
pip3 install cryptography 2>/dev/null
python3 -c "from cryptography.fernet import Fernet; print('ENCRYPTION_KEY:', Fernet.generate_key().decode())"
```

把输出的两个密钥记下来，然后编辑 `.env`：

```bash
nano .env
```

按照下面的模板填写（用方向键移动光标，直接打字修改）：

```env
DATABASE_URL=postgresql+asyncpg://postgres:mydbpass123@postgres:5432/cf_analyzer
POSTGRES_PASSWORD=mydbpass123

JWT_SECRET=刚才生成的那串hex
ENCRYPTION_KEY=刚才生成的那串key

GITHUB_CLIENT_ID=第5步拿到的Client_ID
GITHUB_CLIENT_SECRET=第5步拿到的Client_Secret

ADMIN_GITHUB_IDS=你的GitHub数字ID

DEFAULT_LLM=claude
DEFAULT_LLM_KEY=你的Claude_API_Key（如果想给所有用户提供默认LLM）

MAX_CHROME_INSTANCES=2
CF_HEADLESS=true

ALLOWED_ORIGINS=http://你的IP:6010
```

编辑完按 `Ctrl+O` 保存，`Ctrl+X` 退出。

---

## 第 7 步：启动！（5 分钟）

```bash
cd /root/cf_analyzer_web
docker compose up -d --build
```

第一次会下载镜像和安装依赖，大约 3-5 分钟。看到：

```
✔ Container cf_analyzer_web-postgres-1  Started
✔ Container cf_analyzer_web-app-1       Started
```

查看日志确认启动成功：

```bash
docker compose logs -f app
```

看到 `Uvicorn running on http://0.0.0.0:6010` 就成功了。按 `Ctrl+C` 退出日志。

---

## 第 8 步：初始化数据库（1 分钟）

```bash
docker compose exec app python3 -c "
import asyncio
from db import init_db
asyncio.run(init_db())
print('OK - tables created')
"
```

看到 `OK - tables created` 就完成了。

---

## 第 9 步：建立共享 CF 会话（5 分钟）

这一步让服务器上的 Chrome 登录 Codeforces，保存 session。

```bash
# 安装虚拟显示 + VNC
apt install -y xvfb x11vnc

# 启动虚拟显示
Xvfb :99 -screen 0 1366x768x24 &
export DISPLAY=:99

# 启动 VNC 服务（监听 5900 端口）
x11vnc -display :99 -nopw -forever -rfbport 5900 &
```

现在在你的 **Windows 电脑** 上：

1. 下载 VNC Viewer：https://www.realvnc.com/en/connect/download/viewer/
2. 安装并打开，连接地址填：`你的IP:5900`
3. 你会看到一个黑色桌面（这是服务器的虚拟屏幕）

回到服务器 SSH 窗口，启动 Chrome：

```bash
docker compose exec -e DISPLAY=:99 app python3 -c "
from cf_client import CFClient
from pathlib import Path
client = CFClient('', delay=0, profile_dir=Path('data/cf_browser_profiles/shared'))
page = client._get_page()
page.goto('https://codeforces.com/enter')
input('Press Enter after login...')
client.close()
print('Session saved')
"
```

在 VNC Viewer 里你会看到 Chrome 打开了 Codeforces 登录页。**手动登录你的 CF 账号**，完成 Turnstile 验证。

登录成功后，回到 SSH 窗口按 **回车**。看到 `Session saved` 就好了。

清理 VNC：

```bash
killall x11vnc Xvfb 2>/dev/null
```

> **注意**：如果 VNC 方式搞不定，可以暂时跳过这一步。用户仍然可以登录、配置、拉取提交。只有"分析"功能（需要抓源码）依赖 CF 会话。

---

## 第 10 步：测试（2 分钟）

打开浏览器访问：`http://你的IP:6010`

1. 看到登录页 → 点击 GitHub 登录 → 授权 → 进入主界面
2. 右上角显示你的 GitHub 头像
3. 配置页 → 填 CF Handle → 保存
4. 点击冷启动 → 拉取提交
5. 导航栏出现"管理"tab（因为你是管理员）

**如果到这里一切正常，核心功能已经跑通了！**

---

## 第 11 步（可选）：绑定域名 + HTTPS

如果你有域名（比如 `cf.example.com`）：

### 11a. 域名解析

去你的域名服务商（如 Cloudflare / 阿里云 DNS），添加一条 A 记录：

| 类型 | 名称 | 值 |
|---|---|---|
| A | cf | 你的IP |

### 11b. 配置 Nginx

```bash
cat > /etc/nginx/sites-available/cf-analyzer << 'EOF'
server {
    listen 80;
    server_name cf.example.com;

    location / {
        proxy_pass http://127.0.0.1:6010;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300;
        proxy_send_timeout 300;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF

ln -sf /etc/nginx/sites-available/cf-analyzer /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

### 11c. 申请 SSL 证书

```bash
certbot --nginx -d cf.example.com
```

按提示填邮箱、同意条款，自动配好 HTTPS。

### 11d. 更新配置

```bash
# 更新 .env
nano /root/cf_analyzer_web/.env
# 把 ALLOWED_ORIGINS 改成 https://cf.example.com

# 重启应用
cd /root/cf_analyzer_web && docker compose restart app
```

然后去 GitHub OAuth App 设置页，把回调 URL 改成 `https://cf.example.com/auth/callback`。

---

## 日常操作速查

```bash
# 查看状态
docker compose ps

# 查看日志
docker compose logs -f app

# 重启
docker compose restart app

# 更新代码后重新部署
docker compose up -d --build

# 备份数据库
docker compose exec postgres pg_dump -U postgres cf_analyzer > backup.sql

# 恢复数据库
cat backup.sql | docker compose exec -T postgres psql -U postgres cf_analyzer

# CF 会话过期后重建：重复第 9 步
```

## 故障排查

| 现象 | 解决 |
|---|---|
| 打不开页面 | 检查防火墙是否开了 6010 端口；`docker compose ps` 看容器是否运行 |
| GitHub 登录后跳回登录页 | 检查 OAuth App 的 callback URL 是否正确 |
| 分析报"服务器繁忙" | Chrome 实例占满了，等一会或调大 `MAX_CHROME_INSTANCES` |
| 分析报"CF 会话无效" | 共享 session 过期，重新执行第 9 步 |
| 数据库连接失败 | `docker compose restart postgres` |
