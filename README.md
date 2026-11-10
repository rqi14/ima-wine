# ima-wine

在 Docker 里运行腾讯 [ima](https://ima.qq.com) 桌面客户端（Windows 版，跑在 Wine 上），通过 MCP 让 AI agent
搜索、阅读你账号下的 ima 知识库，包括**订阅知识库**——它们的原文网页版和 OpenAPI 都读不到。

- 列出 / 浏览 / 全文检索个人、共享、订阅知识库；凭分享链接订阅新知识库
- 取文档全文（按页分隔）和 PDF 原件，可直接共享给 agent 的容器
- 调用知识库自带的 AI 问答
- 微信扫码登录；掉线时工具直接返回二维码
- 无界面运行，常驻内存约 0.5–1 GB

## 快速开始

```bash
mkdir ima && cd ima
curl -O https://raw.githubusercontent.com/rqi14/ima-wine/main/docker-compose.yml
docker compose up -d
```

首次启动会从腾讯官方地址下载 ima 安装包（约 220 MB）并初始化 Wine，需要一两分钟：

```bash
docker logs -f ima          # 看到 "starting ima" 即就绪
```

登录：

```bash
docker exec ima imactl qr -o /data/qr.png   # 二维码保存在 ./data/qr.png，用微信扫码
docker exec ima imactl status               # "logged_in": true
```

登录状态保存在 `./data` 里，重启、重建容器都不用重新扫码。停止容器请用 `docker compose down` / `docker stop`，不要 `kill`。

在 Windows / macOS 的 Docker Desktop 上，`./data` 这种挂主机目录的方式会让 Wine 卡住，请改用命名卷
（`- ima-data:/data`，并在文件末尾声明 `volumes: {ima-data: {}}`）。

## 接入 agent

MCP 端点：`http://127.0.0.1:8765/mcp`（streamable HTTP）。

```bash
# Claude Code
claude mcp add --transport http ima http://127.0.0.1:8765/mcp
```

```yaml
# Hermes Agent（config.yaml）
mcp_servers:
  ima:
    url: http://127.0.0.1:8765/mcp
    timeout: 600
```

第一次读取一份大文档要在客户端里打开并下载，可能需要几十秒，客户端侧的超时请设长一些。

| 工具 | 作用 |
|---|---|
| `ima_list_knowledge_bases` | 所有知识库：个人、共享、订阅（实时，别处刚订阅的也在） |
| `ima_subscribe(link)` | 凭分享链接（`https://ima.qq.com/wiki/?shareId=…`）订阅知识库；需库主审核的返回「待审核」，付费库不处理 |
| `ima_list(kb, folders)` | 某个库或文件夹里的条目 |
| `ima_search(kb, query, limit)` | 库内全文检索，返回标题与命中片段 |
| `ima_get_document(kb, title, pages, max_chars)` | 文档全文，按页分隔；超长时分段返回 |
| `ima_render_pages(title, pages)` | 指定页的图片，用于图表、扫描件 |
| `ima_ask(kb, question)` | 知识库自带的 AI 问答 |
| `ima_status` / `ima_login_qr` / `ima_login_wait` | 登录状态与扫码登录 |
| `ima_screenshot` | 客户端截图，排查问题用 |

库名、标题都可以只写开头一段。

### 把 PDF 原件交给 agent

`ima_get_document` 下载的原件保存在 `./data/downloads`。如果 agent 跑在另一个容器里，把同一个目录挂进去，并告诉
ima 它在 agent 那边的路径，返回结果里就会带上 agent 能直接打开的文件路径：

```yaml
    environment:
      IMA_DOWNLOADS_PUBLIC: /workspace/ima-downloads
    volumes:
      - ./data:/data
      - /srv/agent/ima-downloads:/data/downloads
```

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `IMA_URL`, `IMA_MD5` | 2.6.16 官方安装包 | 换 ima 版本时两个一起改 |
| `IMA_DOWNLOADS_PUBLIC` | 空 | agent 看到的下载目录路径，见上 |
| `IMA_DROP_EXTENSIONS` | 10 个内置扩展 | 不加载的 ima 内置扩展（录音、播客、脑图等，省内存）；设为空则全部保留 |
| `IMA_KSM` | `1` | 让进程参与内核同页合并（KSM），需宿主开启，见下 |
| `SCREEN` | `1600x1000x24` | 虚拟屏幕尺寸 |

| 端口 | 用途 |
|---|---|
| 8765 | MCP |
| 9223 | 客户端的 Chrome DevTools 协议，调试用，默认不发布 |

## 资源占用

内存上限建议 2.5 GB。实测启动峰值约 1 GB，空闲约 0.5–0.8 GB，打开 PDF 时再多 0.3 GB 左右。

宿主开启 KSM 可再省约 0.4 GB（容器内无法开启）：

```bash
echo 1 > /sys/kernel/mm/ksm/run
echo scan-time > /sys/kernel/mm/ksm/advisor_mode    # 内核 ≥ 6.8：自动调节扫描速度
```

## 工作原理

- **Wine**：自行编译的 Wine 11.21，打了三个补丁（`wine/`）。原版 Wine 下 Chromium 类应用的每个进程要多占
  数百 MB，ima 启动时峰值可达十几 GB；补丁让 `chrome.dll` 的只读段及其重定位后的页在进程间共享，并避免页保护表
  被整片写入（移植自 [WineHQ bug 58805](https://bugs.winehq.org/show_bug.cgi?id=58805)）。另修了一个
  `shell32` 在文件移动失败时的崩溃。
- **客户端**：ima 只在容器首次启动时下载，镜像里不包含它。客户端用 ima 自带的 `TencentRemoteDebugSwitch`
  开启调试端口，`imactl` 通过 Chrome DevTools 协议驱动它。列库、浏览、搜索、订阅、打开文档调用的是客户端知识库
  页面自己的服务（页面上按钮背后的同一批请求，由页面用自己的登录态发出），不点界面、不等渲染；取原文仍是在客户端
  阅读器里按「下载」。知识库问答（`ima_ask`）还是在界面上输入问题、读回答。
- **文字层**：下载后用 `pdftotext` 提取全文。思源黑体 / 宋体子集的 ToUnicode 常常损坏，按 Adobe 各发布版本的
  CID 表修复（`imactl/cidmaps`，由 `tools/build_cidmaps.py` 生成），修复直接写回 PDF。

## 已知限制

- 库主设为「不可查看」的文件取不到原件，`ima_get_document` 会说明原因，可改用 `ima_ask`。
- 扫描件，以及正文被栅格化成图片的 PDF（常见于 Aspose 由 Office 转出的文件），没有可用的文字层。这些页会标在
  `image_only_pages` 里，可用 `ima_render_pages` 交给视觉模型；结构树里若有替代文本会一并返回。
- 所有操作串行执行。
- 依赖 ima 页面内部的服务名（如 `READER_SERVICE`），升级 ima 版本后可能要跟着改。
- 只支持 x86_64。

## 从源码构建

```bash
docker build -t ghcr.io/rqi14/ima-wine:latest .
```

Wine 全量编译在 32 核机器上约 15 分钟。构建好的镜像可以直接传到运行它的机器：

```bash
docker save ghcr.io/rqi14/ima-wine:latest | gzip | ssh <host> "gunzip | docker load"
```

`imactl` 也可以在容器里当命令行用：`docker exec ima imactl status|qr|kbs|ls|search|get|screenshot|close`。

## 许可

代码与 Wine 补丁采用 LGPL-2.1-or-later（与 Wine 一致）。ima 是腾讯的软件，本项目不分发它；知识库内容的使用遵循
其原有权利约束。
