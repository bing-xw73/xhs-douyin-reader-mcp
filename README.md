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
| `watch_douyin(url, ask)` | 百炼全模态模型代看完整视频或最多 9 张图文图片，仅返回文字概要；可指定问题 |
| `read_douyin_images(url, indexes)` | 读取抖音 `/note/`、`/slides/` 图文帖图片 |

图片会转成 JPEG，质量约 85，长边不超过 1568 像素。特别长的图片会纵向切成多段，尽量保证文字清晰。

视频下载和处理限制为 **6 分 30 秒、150 MB**、最大约 1080p 像素量；默认截取 4 帧，最多 8 帧。下载时边读取边写磁盘，不把完整视频放进内存。处理成功或失败后都会立即删除临时视频和音频。图文帖即使带背景音乐也不会转写。

## 适合谁

这份说明以 Ubuntu 24.04 VPS 为例。你需要准备：

- 一台有公网 IP 的 Linux 服务器；
- 一个指向服务器的域名；
- 能登录服务器执行命令；
- 如果需要语音转写，再准备一个硅基流动 API Key；
- 如果需要概要模式，再准备对应地域的阿里云百炼 API Key（默认北京地域）。

没有 API Key 也可以读取正文、图片和视频帧；`transcribe=true` 需要硅基流动 key，`watch_douyin` 需要百炼 key。

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

需要概要模式时，继续在同一个 `.env` 文件填写：

```dotenv
OMNI_API_KEY=<你的百炼_API_Key>
OMNI_MODEL=qwen3-omni-flash
OMNI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OMNI_CACHE_DIR=/var/lib/xhs-douyin-reader-mcp/watch-cache
```

默认接口对应北京地域；其他地域请按百炼官方文档修改 `OMNI_BASE_URL`，key 必须与接口地域匹配。key 应完整复制到等号后的一行，不要发送到聊天或日志中。示例 systemd 会创建可写缓存目录；手动启动时请把 `OMNI_CACHE_DIR` 改为自己有写权限的目录。配置文件权限保持 `600`。

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

## 概要模式与长视频

日常只想知道“这个视频讲了什么”时，优先调用 `watch_douyin`，这样 Claude 收到的只有元信息和文字描述。需要亲眼查看画面细节时，再调用 `read_douyin_frames`。

```text
请用 watch_douyin 看这个抖音链接，重点回答它介绍了哪些英语学习方法。
```

- 最长处理 390 秒（6 分 30 秒）、150,000,000 字节（150 MB）的视频，仍保留约 1080p 的原视频像素量安全限制。
- 概要模式会观看完整时间范围：等比例压至 480p，2 分钟内为每秒 2 帧，超过 2 分钟为每秒 1 帧；随视频发送 AAC 48k 音频。抽样帧率意味着极短的一闪而过画面可能没有被观察到。
- 按 `qwen3-omni-flash` 的单次 150 秒及 Base64 小于 10 MB 的限制，每段最长 120 秒，文件仍过大就继续切短。分段观察后再请求模型合并，没有只看前 120 秒。
- 模型用中文输出“画面 / 声音 / 台词”，尽量照抄清晰字幕；短内容最多 300 字，分段合并最多 500 字。输出会标注“以下是全模态模型代看的结果”，仍可能存在识别错误。
- 图文帖最多发送前 9 张图片统一描述，不下载或转写背景音乐。需检查指定图片时使用 `read_douyin_images`。
- 语音转写提取单声道 16k 音频，按接口大小、时长限制分段拼接。错误会明确返回，不用空内容冒充成功。
- 概要按“作品 ID + ask 内容”在磁盘缓存 7 天，只保存文字元信息和描述。不同问题分别缓存；缓存调用同样计入每分钟 5 次抖音工具调用限制。
- 概要和图像/视频处理共用一个任务锁。长视频常需数分钟；服务器以 Streamable HTTP 的 SSE 响应和心跳保持连接，nginx 示例已关闭缓冲。客户端若有更短的工具超时，需调整其超时设置。

百炼输入方式及限制见[官方 Qwen-Omni 文档](https://help.aliyun.com/zh/model-studio/qwen-omni)，语音接口规格见[硅基流动官方文档](https://docs.siliconflow.cn/docs/api/audio-transcriptions-post)。更换模型后仍采用上述保守分段限制。

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
- 原有工具成功结果在内存缓存 10 分钟；概要文字缓存 7 天，服务重启后仍可使用；
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
- 语音转写会把音频发送给硅基流动；概要模式会把压缩的视频及音频或图片发送给百炼。两者可能产生 API 费用，使用前请阅读相应平台的隐私政策和计费说明。
- 连接器 URL 含有鉴权随机串，必须保密。若泄露，请立即更换 `MCP_SECRET` 并重启服务。

## 用 Cloudflare 配置 HTTPS（推荐新手）

如果你的域名 DNS 托管在 Cloudflare，可以让 Cloudflare 负责对外提供 HTTPS，服务器只监听 80 端口，不需要安装证书：

1. 登录 Cloudflare，确认域名已经接入 Cloudflare 并使用 Cloudflare 提供的名称服务器。
2. 在 **DNS** 页面新增一条 A 记录，名称填写准备给 MCP 使用的子域名，IPv4 地址填写 VPS 的公网 IP。
3. 打开这条记录的**代理状态**，确保显示橙色云朵。
4. 打开 **SSL/TLS → 概述**，将加密模式设置为**灵活（Flexible）**。
5. 按前面的 nginx 步骤配置站点，并让 nginx 监听 80 端口即可，不需要执行 Certbot 命令。

等待 DNS 生效后，连接器仍然使用 `https://你的域名/MCP_SECRET/mcp`。Cloudflare 的“灵活”模式会加密浏览器或 MCP 客户端到 Cloudflare 的连接，但 Cloudflare 到源站服务器之间使用 HTTP，因此更适合操作简单的个人低频部署。

## 常见问题

### 在 Windows 上怎么登录服务器？

打开 PowerShell，运行：

```powershell
ssh root@你的服务器IP
```

第一次连接时按提示确认服务器指纹。输入密码时屏幕不会显示字符、圆点或星号，这是正常现象；输入完成后直接按回车。

### 怎么分辨当前在哪台机器上？

看到类似 `PS C:\>` 的提示符，说明命令正在自己的 Windows 电脑上运行。看到类似 `root@服务器名:~#` 的提示符，说明已经进入服务器。执行命令前先看清提示符，避免把命令粘贴到错误的窗口。

### 连接器的 Authentication 怎么选？

选择 **No sign-in**。本项目使用连接器 URL 中的随机路径作为简单鉴权。

### 服务更新后，在 claude.ai 里看不到新工具怎么办？

先断开自定义连接器，再重新连接，然后新建一个对话。已有对话可能仍然保留旧的工具列表。

### 工具返回“需要登录”或“需要验证”怎么办？

这通常表示服务器 IP 被平台登录墙、验证码或风控拦截。请隔一段时间再试，不要连续重试，否则限制可能持续更久。

## 致谢

本项目在 Claude 与 Codex 的协助下完成。Claude 和 Codex 是本项目的主要贡献者。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

测试以本地构造的数据为主，不会在默认测试流程中请求小红书、抖音或语音识别服务。

## License

[MIT](LICENSE)
