# 小红书 / 抖音读取 MCP

一个可以自行部署的远程 MCP 服务器，用于在 Claude、ChatGPT 等支持 Streamable HTTP MCP 的客户端中读取公开的小红书和抖音内容。

项目只处理公开分享页，不登录账号、不执行网页 JavaScript，也不尝试绕过验证码或风控。若平台要求登录或验证，工具会返回明确错误。

## 功能

| 工具 | 用途 |
| --- | --- |
| `read_xhs_post(url)` | 读取小红书标题、正文、作者、标签、互动数据、发布时间和图片链接 |
| `read_xhs_images(url, indexes)` | 按需读取小红书图片；默认前 2 张，最多 4 张 |
| `read_douyin_video(url)` | 识别抖音视频或图文帖，读取文案、作者、互动数据、时长、封面或图片数量 |
| `read_douyin_frames(url, count, transcribe)` | 抖音视频均匀抽帧；可选调用硅基流动语音识别 |
| `read_douyin_images(url, indexes)` | 读取抖音 `/note/`、`/slides/` 图文帖图片 |

图片会转成 JPEG，质量约 85，长边不超过 1568 像素。特别长的图片会纵向切成多段，尽量保证文字清晰。

视频限制为 5 分钟、100 MB、最大约 1080p 像素量；默认截取 4 帧，最多 8 帧。处理完成后立即删除临时视频和音频。图文帖即使带背景音乐也不会转写。

## 适合谁

这份说明以 Ubuntu 24.04 VPS 为例。你需要准备：

- 一台有公网 IP 的 Linux 服务器；
- 一个指向服务器的域名；
- 能登录服务器执行命令；
- 如果需要语音转写，再准备一个硅基流动 API Key。

没有 API Key 也可以读取正文、图片和视频帧，只是不能使用 `transcribe=true`。

## 一、安装

先登录服务器，安装系统依赖：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv nginx ffmpeg
```

把仓库下载到服务器：

```bash
sudo git clone https://github.com/bing-xw73/xhs-douyin-reader-mcp.git /opt/xhs-douyin-reader-mcp
cd /opt/xhs-douyin-reader-mcp
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
```

## 二、填写配置

复制示例配置：

```bash
sudo cp .env.example .env
sudo chmod 600 .env
```

生成一段随机路径：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

编辑配置：

```bash
sudo nano .env
```

至少修改下面几项：

```dotenv
MCP_SECRET=<刚才生成的随机字符串>
MCP_PUBLIC_HOST=reader.example.com
MCP_ALLOWED_HOSTS=reader.example.com,localhost:18120
MCP_ALLOWED_ORIGINS=https://claude.ai,https://chatgpt.com,https://reader.example.com
```

把 `reader.example.com` 换成你自己的域名。需要转写时再填写：

```dotenv
SILICONFLOW_API_KEY=<你的硅基流动_API_Key>
```

不要把 `.env` 上传到 GitHub，也不要把完整连接器 URL 发到公开群聊或截图里。路径中的 `MCP_SECRET` 相当于访问密码。

## 三、启动服务

安装 systemd 服务：

```bash
sudo cp deploy/xhs-douyin-reader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xhs-douyin-reader
sudo systemctl status xhs-douyin-reader --no-pager
```

看到 `active (running)` 表示后端已经启动。它默认只监听服务器本机的 `localhost:18120`，不会直接暴露新端口。

## 四、配置域名和 HTTPS

先在域名服务商处新增一条 A 记录：

- 主机名：例如 `reader`
- 地址：你的 VPS 公网 IP

然后复制 nginx 示例：

```bash
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/xhs-douyin-reader
sudo nano /etc/nginx/sites-available/xhs-douyin-reader
```

把文件中的 `reader.example.com` 全部换成你的域名，再启用：

```bash
sudo ln -s /etc/nginx/sites-available/xhs-douyin-reader /etc/nginx/sites-enabled/xhs-douyin-reader
sudo nginx -t
sudo systemctl reload nginx
```

远程 MCP 必须使用 HTTPS。可以用 Cloudflare 代理，也可以安装 Certbot 签发证书。选择 Certbot 时：

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d reader.example.com
```

最终连接器地址是：

```text
https://你的域名/MCP_SECRET/mcp
```

其中 `MCP_SECRET` 替换为 `.env` 中的真实随机字符串。

## 五、接入 claude.ai

1. 打开 Claude 网页版的设置。
2. 找到 Connectors / 连接器，选择添加自定义连接器。
3. 名称可以填写“小红书和抖音读取”。
4. URL 填写完整的 `https://你的域名/MCP_SECRET/mcp`。
5. 本项目使用随机路径作为简单鉴权，不需要填写 OAuth Client ID 或 Secret。
6. 保存后新建对话，在工具列表中启用该连接器。

可以先这样测试：

```text
请调用 read_xhs_post 读取这个公开小红书链接，只返回标题和作者。
```

需要看图时再调用 `read_xhs_images`，这样比较节省上下文额度。

## 更新

```bash
cd /opt/xhs-douyin-reader-mcp
sudo git pull --ff-only
sudo .venv/bin/pip install -r requirements.txt
sudo systemctl restart xhs-douyin-reader
```

## 安全设计

- 只接受小红书、抖音及其明确允许的 CDN 域名；
- 每次重定向都重新校验域名和公网 IP，降低被当作代理或 SSRF 的风险；
- 小红书和抖音各自限制为每分钟最多 5 次工具调用；
- 成功结果只在内存缓存 10 分钟；
- 抖音匿名 `ttwid` 只保存在内存，服务重启后消失；
- API Key 只从环境变量读取，不写入代码或返回内容；
- nginx 示例关闭访问日志，避免随机鉴权路径进入日志；
- 视频和音频位于 systemd 私有临时目录，成功或失败都会清理。

## 注意事项

- 本项目仅适合个人、低频、小规模使用，不是批量采集工具。
- 请遵守小红书、抖音的服务条款、平台规则、版权规则和当地法律。
- 只处理你有权访问和使用的公开内容，不要绕过登录、验证码、付费或隐私限制。
- 平台页面结构和风控规则会变化，今天可读的链接以后可能失败。
- 海外 VPS、机房 IP 或高频访问更容易触发限制。
- 语音转写会把提取出的音频发送给硅基流动，并可能产生费用；使用前请阅读其隐私政策和计费说明。
- 连接器 URL 含有鉴权随机串，必须保密。若泄露，请立即更换 `MCP_SECRET` 并重启服务。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

测试以本地构造的数据为主，不会在默认测试流程中请求小红书、抖音或语音识别服务。

## License

[MIT](LICENSE)
