# ima-wine

在 Linux 容器里用 Wine 跑腾讯 **ima.copilot** Windows 客户端，并提供 `imactl` 让 agent 操作它。

## 为什么需要

ima 网页版打开**订阅知识库**里的 PDF 会提示「版本过低」。原因是网页代码只在客户端原生接口
`chrome.imaFrame` 存在、且客户端版本 ≥ 2.5.0 时才走加密通道 `s/file_manager/get_media`，
加解密在客户端的原生代码里完成。ima 只有 Windows / macOS 版，所以在 Linux 上只能跑官方客户端。

本项目**不**伪造客户端、不碰它的加密接口、不改它的二进制（它有签名自校验）。客户端在 Wine 里原样运行，
`imactl` 只做用户在界面上能做的事：点击条目、按阅读器自带的「下载」按钮、看屏幕。
客户端不提供下载的文档，就只能在阅读器里读。

## 组成

| 路径 | 作用 |
|---|---|
| `wine/0001-cef-memory.patch` | Wine 11.21 的 Chromium 内存补丁（见下） |
| `wine/0002-shell32-null-release.patch` | 修 Wine `IFileOperation` 移动失败时的空指针崩溃（ima 下载出错会整个崩掉） |
| `wine/0003-share-relocated-copies.patch` | 共享副本按 wineserver 分配的基址预先重定位，重定位后的页也跨进程共享 |
| `Dockerfile` | 编译打补丁的 Wine（只要 x86_64）+ 运行时；**镜像里不含 ima** |
| `docker/entrypoint.sh` | 首次启动从腾讯官方地址下载 ima（校验 md5）、Xvfb、启动、优雅退出 |
| `docker/ksm_preload.c` | 让每个进程参与 KSM 同页合并 |
| `imactl/ima_client.py` | 核心：经 CDP 像用户一样操作客户端，取全文 |
| `imactl/server.py` | MCP 服务（streamable HTTP，`:8765/mcp`），给其他容器里的 agent 用 |
| `imactl/imactl.py` | 调试用 CLI |
| `imactl/cidmaps/` + `tools/build_cidmaps.py` | 思源字体 CID → Unicode 表，修复坏文字层用（见「已知问题」） |

## 内存

Chromium 系应用在原版 Wine 下每个渲染进程要 600–1000 MB，ima 2.6.16 启动峰值 17.8 GB。两个根因
（[WineHQ bug 58805](https://bugs.winehq.org/show_bug.cgi?id=58805) 的分析）：

1. Chromium 预留 ~1.3 TB `PAGE_NOACCESS` 地址空间，Wine 的页保护表被整片写零，每进程 ~330 MB。
2. `chrome.dll` 文件对齐 0x200，Wine 无法 mmap，每个进程 pread 一份私有副本，~220 MB。

补丁 1 移植自该 bug 的原型补丁（作者 John Goodman），另加一处：共享副本放磁盘临时文件而非 memfd，
让这部分成为可回收的页缓存。再加上入口脚本里的两项：不恢复上次会话、去掉 10 个 agent 用不到的内置扩展
（每个扩展启动时、以及每开一个新标签页时都会拉起一个 ~40 MB 的进程）。

实测（ima 2.6.16，登录后，匿名 + shmem，不含可回收的文件缓存）：

| | 启动峰值 | 稳定后 |
|---|---|---|
| 原版 Wine 11.21 | 17.8 GB | — |
| 补丁 | 1.86 GB | 1.14 GB |
| 补丁 + 去扩展 | 1.50 GB | 1.13 GB |
| 补丁 + 去扩展 + KSM | 1.43 GB | 0.75 GB |
| 补丁（含 0003）+ 去扩展 | 1.11–1.18 GB | 0.88–0.94 GB |
| 补丁（含 0003）+ 去扩展 + KSM（2 vCPU / 6 GB 的 VM 上实测） | **0.97 GB** | **0.54 GB** |

0003 的来由：`chrome.dll` 不在首选基址上，每个进程都要把共享副本重定位一遍，被写脏的 ~18 MB
就不再共享（启动时 ~40 个进程）。wineserver 给每个文件分配固定的映射地址，于是可以在生成共享副本时
按这个地址预先重定位；客户端确实映射在该地址时跳过这些重定位块，否则退回私有副本。
实测每个渲染进程被写脏的共享副本页 18.2 MB → 40 KB。

作为对照：同一时刻 Windows 上的 ima 提交大小（私有字节）1.13 GB，任务管理器显示 443 MB
（任务管理器只算常驻、未压缩、不共享的部分）。容器上限给 2.5 GB 足够，开 PDF 阅读器时会再涨 ~0.3 GB。

KSM 要在**宿主机**打开（容器里改不了）：

```bash
echo 1 > /sys/kernel/mm/ksm/run
# 内核 ≥ 6.8 可交给 advisor 自动调扫描速度
echo scan-time > /sys/kernel/mm/ksm/advisor_mode
```

## 运行

```yaml
services:
  ima:
    image: ghcr.io/rqi14/ima-wine:latest
    container_name: ima
    restart: unless-stopped
    mem_limit: 2500m
    shm_size: 512m
    stop_grace_period: 40s        # 让 ima 来得及把轮换后的登录令牌写盘
    ports: ["127.0.0.1:8765:8765"]   # MCP；调试 CDP 再加 9223
    environment:
      IMA_DOWNLOADS_PUBLIC: /opt/data/ima-downloads   # agent 容器里看到的下载目录，写进返回的 file 路径
    volumes:
      - ./ima-data:/data          # Wine 前缀、ima 本体、登录状态
      - ./agent-data/ima-downloads:/data/downloads    # 下载的原件；agent 那边挂同一个目录
```

原件直接共享给 agent：它要查表格、看图、用自己的工具时打开文件即可；`ima_get_document` 的文字只是省事的那一层。

- 首次启动会下载 ~220 MB 的官方安装包并初始化 Wine 前缀，约 1–2 分钟。
- 换 ima 版本：设 `IMA_URL` 和 `IMA_MD5`（地址可在 ima 自己更新器的日志里找到）。
- 保留全部内置扩展：`IMA_DROP_EXTENSIONS=""`。

### 登录

```bash
docker exec ima imactl qr -o /data/qr.png     # 用微信扫 ima-data/qr.png；或让 agent 调 ima_login_qr
docker exec ima imactl status
```

登录状态存在前缀里。**不要硬杀容器**：ima 每次启动都会轮换登录令牌，而 Chromium 是延迟写盘的，
硬杀会留下已作废的旧令牌，下次就得重新扫码。`docker stop` 会走入口脚本里的 `Browser.close`。

## 接入 agent（MCP）

容器发布 `8765`，端点 `/mcp`。hermes 的 `config.yaml`：

```yaml
mcp_servers:
  ima:
    url: http://127.0.0.1:8765/mcp
    timeout: 600        # 首次取一份大文档要走一遍界面 + 下载
```

| 工具 | 作用 |
|---|---|
| `ima_status` | 是否登录、客户端版本 |
| `ima_login_qr` | 新的微信登录二维码（图片），转发给用户扫 |
| `ima_login_wait` | 等用户扫完 |
| `ima_list_knowledge_bases` | 全部知识库：个人、共享、订阅 |
| `ima_search(kb, query)` | 库内全文检索（客户端自带搜索，订阅库 / 共享库也行），返回标题与片段 |
| `ima_list(kb, folders)` | 某库 / 某文件夹下的条目 |
| `ima_get_document(kb, title, folders, pages, max_chars)` | **全文**，按页分隔，外加 agent 可直接打开的 PDF 路径；不给 `folders` 就用库内搜索定位；超长时告诉从哪页接着要 |
| `ima_render_pages(title, pages)` | 指定页的图片（乱码页、扫描件、图表） |
| `ima_screenshot` | 整个屏幕，排错用 |

- **全文怎么来**：在客户端里点开文档，按阅读器自带的「下载」，拿到原文件后 `pdftotext` 抽全文，阅读器随即关掉。
  客户端不给下载的文档，就把阅读器从头滚到尾、等每页渲染完再收集文字层。agent 只看到全文，看不到阅读器。
- **缓存**：全文按页缓存在 `/data/downloads/.text/`，同一份文档第二次起直接返回。
- **登录**：每个工具先查登录状态；掉线时返回 `NOT_LOGGED_IN` 和一张新二维码，agent 发给用户扫，再调 `ima_login_wait`。
- 名字可以只写前缀；客户端只有一个界面，所有操作串行执行。

调试：`docker exec ima imactl status|qr|screenshot|kbs|ls KB [FOLDER..]|search KB QUERY|get KB [FOLDER..] TITLE|close`。

## 已知问题

PDF 文字层的两类毛病，处理方式不同：

- **思源黑体 / 宋体子集，ToUnicode 坏了或缺了**（中文研报很常见）：抽出来是生僻字（「⹨俚յ䠋」）或干脆丢字。
  子集保留了原始 CID，而完整思源字体有 CID → Unicode 的对应，所以能**精确**修好。CID 编号随版本变，
  连小版本也变（宋体 1.000 → 1.001 把「持」从 19302 挪到 19301），所以 `tools/build_cidmaps.py` 从 Adobe
  每个发布版本（黑体 1.001–2.005、宋体 1.000–2.003，CN 区域子集与 SC 泛 CJK 各一）生成表，
  并顺着 GSUB 替换把异体字形也映射上，去重后 24 张、5.3 MB。下载后逐个字体用每张表解码，
  取解码成常用字（GB2312/Big5）最多、且占比 ≥ 85% 的那张（选对版本 98–99%，选错 ~27%）重写 ToUnicode，
  **直接修 PDF 文件本身**（画面不变），共享给 agent 的就是修好的文件。PDF 原有映射与表一致时不动；
  表里没有的字形写成 U+FFFD，明确表示「未知」，不沿用已证明是错的旧映射。实测一份 35 页、18 个子集字体的报告全部还原。
- **Aspose 由 Office 转出的 PDF，同一个字重复（「机机机机」）**：每段正文都被**栅格化成图片**
  （一块 2×2 的纯色图配一张文字形状的透明遮罩），上面叠一层看不见的 0 号字形，坏 ToUnicode 把它映射成一个字。
  文件里没有这些字。能精确拿回的只有结构树里的 `/Alt`、`/ActualText`（生成器为无障碍写的，图表常有整段原文），
  会附在该页输出里；其余列在 `image_only_pages`，交给 agent 看图。
  试过 Tesseract 整页 OCR：漏标题、多栏串行、水印处错字，不值它的体积，已去掉。

依赖只加了纯 Python 的 pypdf 和 fontTools（pikepdf 会带进 lxml、Pillow，多 ~40 MB）。
- 客户端不给「下载」的文档走阅读器文字层兜底，这条路径还没遇到真实样本。

## 版权

Wine 补丁沿用 Wine 的 LGPL-2.1-or-later。ima 是腾讯的闭源软件，本仓库和镜像都不分发它。
从知识库下载的文档受原权利人约束，仅供账号持有人自己使用。

## 构建与发布

Wine 全量编译在 32 核上约 15 分钟，GitHub Actions 的 4 核机器要一小时左右，所以镜像在本地构建后直接推送：

```bash
docker build -t ghcr.io/rqi14/ima-wine:latest .
docker push ghcr.io/rqi14/ima-wine:latest     # 需要 write:packages 权限
```

`.github/workflows/image.yml` 只保留手动触发，作为没有本地构建机时的后备。
