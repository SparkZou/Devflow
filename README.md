# DevFlow AI — AI Delivery Agent

把"微信/钉钉找需求 → 复制粘贴 → 给 AI 描述 → 写代码 → 部署 → 验证 → 通知客户 → 反复确认"这条链路自动化。
你只在两个地方点一下：**合并 PR** 和 **发给客户**（都可以关掉变成全自动）。

```
微信（复制即收件）──┐
                    ├─► 收件箱 ─► AI 判定是不是需求 ─► 自动生成任务（标题/验收标准/优先级/客户/截止日期/待确认问题）
钉钉机器人（自动）──┘                                          │
                                                               ▼
                                              GitHub Issue 自动创建（含客户原话）
                                                               │
                                                               ▼
                             Claude Code 在独立 worktree 里写代码 ─► 自动 commit / push / 开 PR
                                                               │
                                                               ▼
                                     等 CI ─► 失败自动让 Claude 修一次 ─► 通过后【你点"合并"】
                                                               │
                                                               ▼
                                   合并 ─► 你的 GitHub Actions 部署 ─► 健康检查 ─► Playwright 截图 ─► AI 判定
                                                               │
                                                               ▼
                                生成 Release Note + 客户回复草稿 ─► 【你点"发送"】─► 钉钉直接发 / 微信复制粘贴
                                                               │
                                                               ▼
                                       每天 09:00 / 18:00 提醒你：还有哪些没做、哪些等你确认
```

## 先说结论：可行，但有两个边界

| 环节 | 能否全自动 | 说明 |
|---|---|---|
| 钉钉收需求 | ✅ 全自动 | 官方 Stream 模式机器人，不需要公网 IP，本机长连接 |
| 微信收需求 | ⚠️ 半自动 | 个人微信**没有官方 API**，第三方协议会封号。方案：在微信里 **Ctrl+C 复制聊天记录，系统自动收件**（或粘贴到面板）。企业微信可接 webhook |
| 判定需求 / 生成任务 | ✅ 全自动 | Claude 结构化输出：是否需求、标题、验收标准、P0-P3、客户、项目、截止日期、待确认问题 |
| GitHub Issue | ✅ 全自动 | 用你已登录的 `gh` CLI |
| 写代码 / 提 PR | ✅ 全自动 | Claude Code 无人值守模式（`claude -p`），在独立 git worktree 里跑，不影响你正在改的代码 |
| CI / 自动修 | ✅ 全自动 | 轮询 PR 检查状态；失败拉日志让 Claude 修（次数可配） |
| 合并 | 🔒 默认需确认 | `gates.merge: false` 即全自动 |
| 部署 | ✅ 全自动 | 沿用你现有的 GitHub Actions；系统只是等它跑完并做健康检查 |
| AI 测试 | ✅ 全自动 | Playwright 打开部署地址截图 + 控制台错误 → Claude 看图判定。**只能发现明显问题**，无法替代完整验收 |
| Release Note / 客户回复 | ✅ 全自动 | 生成客户能看懂的更新说明和一条微信/钉钉风格的回复 |
| 通知客户 | 🔒 默认需确认 | 钉钉：一键直接发到原会话；微信：草稿已复制到剪贴板，你 Ctrl+V |
| 提醒自己 | ✅ 全自动 | 桌面通知 + 钉钉/企业微信 webhook 日报 |

AI 调用有两种方式，自动选择：有 `ANTHROPIC_API_KEY` 就走 API；没有就用你本机 **Claude Code 的登录态**（不需要额外 Key）。

---

## 安装（Windows，5 分钟）

> 部署到 Linux 服务器见下文「部署到服务器」。

前提：Python 3.10+、git、[gh CLI](https://cli.github.com)（`gh auth login` 登录过）、VS Code 里装了 Claude Code 扩展或 `npm i -g @anthropic-ai/claude-code`。

```powershell
cd "c:\Development\Projects\NZ\DevFlow AI"
.\scripts\install.ps1          # 建 .venv、装依赖、装 Chromium、生成 config.yaml、跑环境检查
```

手动等价步骤：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\devflow.exe init
```

## 配置 `config.yaml`

最少要填 `projects`（可以写多个）：

```yaml
projects:
  - name: shop-admin
    repo_path: "C:/Development/Projects/shop-admin"   # 本地 clone
    github_repo: "SparkZou/shop-admin"
    default_branch: main
    deploy_url: "https://admin.example.com"           # 没有就留空，跳过 AI 测试
    deploy_workflow: "deploy.yml"                      # 部署的 Actions 文件名；留空则合并后等 deploy_wait_seconds
    customers: ["张总", "李经理"]                       # 帮 AI 把需求归到项目
    keywords: ["后台", "订单", "商品"]
    test_hints: "测试账号 test/123456；订单页在 /orders"
```

其他常用项：

```yaml
gates:            # 哪一步需要你点一下
  merge: true     # 合并前确认（建议保留）
  deliver: true   # 发客户前确认（建议保留）

clipboard:
  enabled: true   # 在微信里复制 = 收件。嫌太敏感可以设 trigger_prefix: "#需求"，只处理以它开头的文本

dingtalk:
  enabled: true
  client_id: "dingxxxx"       # 见下面"接钉钉"
  client_secret: "xxxx"
  notify_webhook: "https://oapi.dingtalk.com/robot/send?access_token=..."   # 给你自己发提醒（可选）
```

检查配置：`.\.venv\Scripts\devflow.exe doctor`，全部 ✅ 即可。

### 一个产品、多个仓库（前后端分离）

每个仓库写成一个项目，`group` 填同一个产品名，`description` 告诉 AI 这个仓库负责什么：

```yaml
  - name: Morphra-frontend
    group: Morphra
    description: "Morphra.ai 前端（React）：页面、UI、交互"
    repo_path: "C:/Development/Projects/Singapore/HomeAura/HomeAuraFrontend"
    github_repo: "SparkZou/HomeAuraFrontend"
    deploy_workflow: "cd.yml"
    deploy_url: "https://morphra.ai"
  - name: Morphra-backend
    group: Morphra
    description: "Morphra.ai 后端（Python API）：接口、数据库、AI 生成逻辑"
    repo_path: "C:/Development/Projects/Singapore/HomeAura/HomeAuraBackend"
    github_repo: "SparkZou/HomeAuraBackend"
    deploy_workflow: "deploy.yml"
    deploy_url: "https://morphra.ai"
```

面板的项目下拉里同一 `group` 只显示一项（"Morphra（2 个仓库）"），你不用管前端还是后端：
AI 分诊时只改一端的需求 → 归到那一个仓库；同时要改前后端的需求（比如"加个页面 + 新接口"）→ 自动拆成两个配套任务
（标题带仓库名，描述里写清各自负责的部分，面板上有 🔗 配套 #N 标记），各自建 Issue、写代码、开 PR、部署，互不阻塞。
你在面板里分别确认合并即可；一般先合后端再合前端。

### 检查类需求（"这个问题是否已经解决了？帮我看看"）

客户不是让你改东西，而是问状态/要结论时，AI 会判成 **检查类**（卡片上 🔍 标记）：Claude Code 在一个只读的临时 worktree 里查代码、`git log`、跑测试、用 WebFetch 看线上，输出"结论 / 依据 / 未解决的话怎么修"的报告，再生成一条给客户的回复。不建 Issue、不开 PR、不动代码。
如果检查结论是"未解决"而你决定要修，在任务页点"重新分析"并在原文后补一句"请修复"，或直接新建一条需求。

### 自己的项目 vs 客户项目

给自己的项目加 `kind: own`，面板下拉会分成"客户项目 / 自己的项目"两组，任务卡片上带"自有"标记，方便你只盯客户的。

## 接钉钉（10 分钟，一次性）

1. 打开 [钉钉开放平台](https://open-dev.dingtalk.com) → 应用开发 → **创建应用**（企业内部应用）。
2. 左侧「添加应用能力」→ 添加 **机器人**，消息接收模式选 **Stream 模式**（不需要填回调地址）。
3. 「凭证与基础信息」里复制 **Client ID (AppKey)** 和 **Client Secret**，填进 `config.yaml` 的 `dingtalk`。
4. 「权限管理」里开通 `qyapi_robot_sendmsg`（机器人发送消息）——用于 sessionWebhook 过期后主动给客户发交付通知。
5. 发布应用（版本管理与发布 → 上线）。
6. 把机器人拉进客户群，或让客户直接单聊它。群里 **@机器人** 发需求即可，单聊直接发。

机器人收到消息后会先回"已记录为 #N，正在分析"，分析完再回任务摘要和需要确认的问题；交付时你点"发送"，回复会发回**同一个会话**。

另外可以在任意一个自己的钉钉群加一个「自定义机器人」，把 webhook 填到 `dingtalk.notify_webhook`，所有提醒和日报会发到那个群（手机上也能看到）。

## 接微信

个人微信没有官方机器人接口，所以：

- **复制即收件**：`devflow serve` 运行时，在微信里选中客户的消息 → Ctrl+C，1 秒内系统自动收到并交给 AI 判断。不是需求的会被标记"非需求"，不打扰你。
- **截图也一样**：在微信里右键复制图片（或任何截图工具复制到剪贴板），会自动附到 3 分钟内刚收的那条任务上；先复制图再复制文字也行，图会等 10 分钟配套文字。AI 分诊和 Claude 写代码时都会看图。
- **面板粘贴**：打开 http://127.0.0.1:8765，在页面任意位置 Ctrl+V——文字进输入框、截图进附件（可多张），然后点"交给 AI"，可以顺手指定项目和客户。钉钉机器人收到图片/图文消息也会自动下载附上。
- **只收微信/钉钉里的复制**：监听按复制时的前台窗口判断，默认只接受 `clipboard.apps` 里的程序（WeChat.exe / Weixin.exe / DingTalk.exe 等），在 VS Code、浏览器里复制的东西不会被当成需求。用了别的微信版本，把进程名加进去即可；设成 `[]` 则全收。
- **回复客户**：交付时草稿自动进剪贴板 + 桌面通知，你切到微信 Ctrl+V 发送。面板里也能先改再复制。
- 企业微信：把群机器人 webhook 填到 `wecom.notify_webhook` 可以收提醒。

## 日常使用

```powershell
.\scripts\start.ps1              # 或 .\.venv\Scripts\devflow.exe serve
```

启动后自动打开面板 http://127.0.0.1:8765（先登录，默认 admin / admin2026，见「面板登录」），三栏：**需要你处理** / **自动进行中** / **最近完成**。

一条需求的一生（默认门禁）：

1. 客户在钉钉发消息 / 你在微信 Ctrl+C → 任务出现在面板 → AI 几秒内判定并整理。
2. 如果 AI 认不出属于哪个项目，任务停在「待建 Issue」等你选项目（下拉选一下 → 创建 Issue）。
3. 之后全自动：建 Issue → Claude 写代码 → 开 PR → 等 CI → CI 失败自动修。
4. CI 通过 → 桌面通知「等你确认合并」→ 面板点 **合并 PR**。
5. 合并 → 等你的 Actions 部署 → 健康检查 → 截图 → AI 判定 → 生成更新说明和回复草稿 → 通知「待发送客户」。
6. 面板看一眼草稿（可改）→ 点 **发送**。钉钉直接发到原会话；微信则复制到剪贴板去粘贴。Issue 自动关闭。
7. 每天 09:00 / 18:00 收到日报：未完成 N 个、等你处理 N 个、逾期 N 个。

命令行等价操作（服务在跑时会自动转发给服务）：

```powershell
devflow add "张总：订单列表加个导出Excel，周五前"   # 手动加需求
devflow list                # 未完成任务
devflow show 12             # 详情
devflow approve 12          # 确认当前步骤
devflow retry 12 --note "请改成后端导出"   # 重试并补充说明
devflow send 12             # 发给客户
devflow ignore 12
devflow digest --send       # 立刻发一份日报
devflow doctor              # 环境检查
```

## 面板登录

面板默认需要登录（`config.yaml` 的 `auth` 段，初始账号 **admin / admin2026**）：

```yaml
auth:
  enabled: true
  username: admin
  password: admin2026      # 部署到公网务必改掉；也可以只在 .env 里写 DEVFLOW_PASSWORD 覆盖
  session_days: 7          # 登录状态保持天数
```

- 浏览器：打开面板跳到 `/login`，登录后 Cookie 会话保持 `session_days` 天，右上角「退出」注销。
- CLI（`devflow add/approve/...`）和 `/api/*`：用同一组账号走 HTTP Basic，CLI 自动带上，不用额外操作。
- 改了密码，所有已登录的会话立即失效；同一 IP 连续输错 5 次锁 60 秒。
- 只在本机用、不想登录：`auth.enabled: false`。

## 部署到服务器（Docker + HTTPS）

代码托管在 <https://github.com/SparkZou/Devflow>，线上地址 <https://devflow.aicloud.co.nz>（服务器 223.165.71.59，`ubuntu` 用户）。

服务器上的约定：应用都是 `/opt/webApp/<name>` 下的 docker compose，80/443 由 `/opt/webApp/caddy` 的**共享 Caddy** 接管并自动签发 HTTPS 证书，所以这里不另装 nginx（会和 Caddy 抢 80/443）；没有共享 Caddy 的机器可用 `deploy/nginx.conf` + certbot。

一键安装（以 `ubuntu` 用户运行，重复运行 = 更新）：

```bash
curl -fsSL https://raw.githubusercontent.com/SparkZou/Devflow/main/deploy/install.sh | bash
```

脚本做的事：clone 到 `/opt/webApp/devflow` → 生成 `conf/config.yaml`（来自 `deploy/config.server.yaml`：剪贴板监听关、面板登录开）和 `.env` → `docker compose up -d --build`（镜像里带 Chromium、git、gh、Claude Code）→ 把 `deploy/Caddyfile.snippet` 追加到共享 Caddy 并 reload → 检查 `https://域名/health`。

装完让流水线能干活还要在容器里登录一次（面板本身不需要）：

```bash
cd /opt/webApp/devflow
docker compose exec devflow gh auth login       # GitHub
docker compose exec devflow claude              # Claude Code 登录；或在 .env 写 ANTHROPIC_API_KEY 后 docker compose up -d
git clone https://github.com/SparkZou/<repo>.git repos/<repo>
vi conf/config.yaml                             # projects 里登记：repo_path: /opt/repos/<repo>
```

日常：

| 事情 | 命令 |
|---|---|
| 更新线上 | `bash /opt/webApp/devflow/deploy/deploy.sh`（push 到 main 且 CI 通过后 Actions 也会自动跑） |
| 看日志 | `cd /opt/webApp/devflow && docker compose logs -f` |
| 重启 | `docker compose restart` |
| 改配置 | 编辑 `conf/config.yaml`，刷新面板即生效；改 `.env` 要 `docker compose up -d` |

自动部署用的是仓库 Variables `DEPLOY_HOST` / `DEPLOY_USER` 和 Secret `DEPLOY_SSH_KEY`（已配置为 `~/.ssh/devflow_ci`）。

## 门禁与安全

- `gates.merge` / `gates.deliver` 默认开着：**代码进主干**和**给客户发消息**这两件事出错代价最大，建议保留。
- Claude Code 在 `data/worktrees/task-N` 里工作，用完即删，**不会碰你正在改的工作目录**。
- 默认 `coder.allowed_tools` 只允许读写文件和 `git/python/pytest/npm` 类命令；想完全放开设 `coder.skip_permissions: true`（风险自负）。
- 所有状态存在 `data/devflow.sqlite3`，日志在 `data/devflow.log`，截图在 `data/screenshots/`。

## 常见问题

**AI 判定慢/失败** — 没有 API Key 时走 `claude -p`，每次 10-30 秒；设置 `ANTHROPIC_API_KEY` 后走 API 会快很多。`doctor` 能看出当前用的后端。

**"Claude 没有产生任何代码改动"** — 需求太模糊或仓库没法本地跑。在面板「重试」时填补充说明（会追加进需求描述），或自己改完 `retry`。

**"Claude Code 未完成：达到最大轮数…有 N 次命令因权限被拒"** — 无人值守模式下不在白名单的命令会被直接拒绝，Claude 会反复重试直到把轮数烧完。看错误里列出的被拒命令，把对应前缀加进 `coder.allowed_tools`（格式 `Bash(xxx *)`），或干脆 `coder.skip_permissions: true`。另外 worktree 路径不能含空格（默认在 `~/.devflow/worktrees`）。

**CI 一直 pending** — 仓库没配 CI 时会直接视为通过；有 CI 但超过 `pipeline.ci_timeout_minutes` 会标记失败。

**部署找不到运行记录** — `deploy_workflow` 填的是 `.github/workflows/` 下的文件名（如 `deploy.yml`），系统按合并后的 commit SHA 匹配；不用 Actions 部署就留空，改用 `deploy_wait_seconds` + 健康检查。

**微信剪贴板误收** — 设 `clipboard.trigger_prefix: "#需求"`，或调大 `min_chars`；收错的点"忽略"即可，也可以关掉 `clipboard.enabled` 只用面板粘贴。

**钉钉发送失败** — `sessionWebhook` 只在收到消息后一段时间内有效；超时后需要应用有 `qyapi_robot_sendmsg` 权限并已发布。

**换电脑 / 开机自启** — 把 `scripts\start.ps1` 加到任务计划程序（登录时运行）即可。
