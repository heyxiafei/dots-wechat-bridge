# dots-wechat-bridge

R7 包含官方额度读取适配与固定命令处理、单协调者持久提交与去重、普通回复优先、有界长图预览和安装接续检查。GitHub 发布、目标实例部署、扫码绑定、API 接受、手机收到分别验证。AIHot 集成默认关闭，必须由受信任宿主显式注入部署配置；发行包不携带私人配置。

已有服务的升级应复用原连接，生产启动以 [安装与接续检查](docs/INSTALLATION.md) 为准。旧 dot 镜像保持只读，固定系统 ACK 关闭，原订阅和唯一协调任务保留；不能直接执行下方通用首次安装示例来跳过旧实例隔离或建立第二个收发者。扫码、接口接受和手机收到分别验收。

项目现在提供[可复现安装与扫码连接入口](docs/INSTALLATION.md)：`scripts/install-project.py install` 创建隔离环境并完成离线启动验收；首次平台接入和单写者切换获准后，`connect` 在本机展示二维码、等待本人确认并启动服务。安装成功、平台接通和手机验收分别记录。

安装后先运行 `scripts/install-project.py preflight --prefix /absolute/new/project --scope connection_check`（使用该安装的 `.venv/bin/python`），一次返回依赖、绑定与配置缺项，再安排扫码。这个只读诊断不读取旧机消息，不执行客户端或收发；已有绑定应保留，不反复扫码。连通、真实新消息入站、一次幂等回复与手机实际收到仍须分别通过，详见安装说明。

若只验新机与微信的一条全新消息，使用 `--scope weixin_transport_check`：它检查已安装的有限诊断与本人绑定，不把完整 DOT/MCP 的隧道文件当作此微信段的前置。预检不启动发送者，仍须核实隔离和本轮本人授权；微信段通过不等于 DOT 已接通。

把自己的微信接到支持 MCP 的 AI 助手。服务器负责收发和私有媒体传输，AI 在自己的产品环境里理解内容、生成回答。服务器不运行模型。

需要验证新机的完整 DOT 链路时，另有[有限 stdio 验收候选](docs/BOUNDED-DOT-STDIO.md)：由 DOT 读取并回复本轮一条新消息，默认只显示计划。它要求实际可用的私有 MCP 传输和原平台事件配置；扫码与本地配置文件不能替代这些授权。候选不会自动转为常驻生产服务，离线测试通过不表示已经部署或手机收到。

这是独立社区项目，未获 OpenAI 或腾讯背书。`dot/dots` 在这里指用户正在使用的助手；仓库没有调用产品内部编排、记忆或隐藏接口。

当前发布版本为 **0.5.0+r7**：简单对话一条完整回复，需要研究、读取附件或执行任务时采用 ack → final 两阶段。回复分支由事件执行方选择，服务器负责固定槽位、顺序和幂等。

## 把链接发给你的 dot

复制下面这段话，把仓库链接发给支持浏览仓库和执行命令的助手：

> 请根据 https://github.com/heyxiafei/dots-wechat-bridge 的 README 和 AGENTS.md，帮我在我授权的主机上完成只与本人微信收发的原桥接项目。先检查系统、执行能力、Secure MCP Tunnel、插件和 MCP Events，再按验证关卡推进，复用原连接、订阅与唯一处理者。我亲自处理必要的平台授权、私下输入凭据、扫描二维码。微信来信先读取核验；简单问题用 reply_weixin_message 发送一条完整 final；需要研究、读取附件或执行的任务先用 reply_weixin_message_phase 发送贴合任务的 ack，完成后发送一条完整 final。旧 dot 镜像只读，不重新开启固定 ACK。根据 reply_state 和原 send_id 恢复，避免重复发送。普通 dot 聊天仅在我明确要求时发送微信。用获准的无敏感附件验收，分别报告接口状态和手机收件证据。

AI 可以检查环境、安装依赖、生成配置、测试和部署。**本人仍需授权目标主机、处理账号权限、创建/私下输入运行密钥、微信扫码并确认收到测试文件。** 只有仓库读取能力、没有终端/SSH/浏览器的助手会给你可执行步骤，不能代替执行或宣称部署成功。

## 功能

额度查询：固定命令“额度／查额度／查一下额度”指向当前 Codex 周额度。候选提供受信任宿主调用的 `OwnerCoordinator.handle_fixed_command` 及 async 入口，串联本人意图核验、已注入官方读取器、四行展示和原幂等 final 槽；`fixed_command` 提示本身不授予执行权。纯格式模块仍不采集或发送，桥自身没有独立直连读取器；仅安装候选不会自动绑定云端事件或取得账户权限。缺读取能力或可信账户映射时明确显示未知、未调用，不追问额度种类。字段与宿主接入条件见[额度展示契约](docs/QUOTA-DISPLAY.md)和[协调者接线](docs/COORDINATOR-WIRING.md)；候选实现与真实上线须分别验收。

| 方向 | 支持内容 |
|---|---|
| 微信 → AI | 精确文字、图片、文件，私有附件 ID、分块读取、图片预览 |
| 微信语音 → AI | 微信上游已经提供的文字转写；没有转写时提示改发文字 |
| AI → 微信 | 简单请求一条文字回答、复杂任务 ack → final；用户明确要求的通知、图片、文件 |
| 身份 | 只接受二维码绑定者的私聊，只向该绑定者发送 |
| 状态 | 持久队列、稳定 ID 去重、API 接受/失败/不确定状态 |

微信侧的 owner-only 限制的是收发对象。MCP 侧隐私依赖 **Tunnel 和插件的访问控制**：桥接没有逐调用者身份认证，任何获准调用该隧道工具的人都可能读取绑定者消息、请求向绑定者发送。使用仅本人可访问的专用私有隧道与插件，核对组织/工作区授权，不向其他账号共享工具访问权限。

图片/文件出站需要把**实际字节**经认证 MCP 分块传到桥接服务器，之后流式加密上传腾讯 CDN。另一个机器上的路径或无法访问的 URL 不能代替文件内容。微信群、多用户收发、原音频识别、自动转发全部聊天不在当前范围。

## 公开源码安装与升级

以下示例取得当前 `main`，先记录实际提交 SHA，再生成**不含 `.git` 的安装目录**。安装目录的真实绝对路径须只含 ASCII 字母、数字、`_`、`.`、`/`、`-`，不能有空格或中文；`render-config.py` 在配置阶段会拒绝其他路径。先选好最终路径，再创建 `.venv`。示例中的目录名若已存在，先换一个新的目录名，不覆盖已有安装：

```sh
git clone https://github.com/heyxiafei/dots-wechat-bridge.git dots-wechat-bridge-source
cd dots-wechat-bridge-source
git rev-parse HEAD  # 保存此完整 SHA，作为本次安装和回退的源码版本
mkdir ../dots-wechat-bridge-reviewed
git archive --format=tar HEAD | tar -xf - -C ../dots-wechat-bridge-reviewed
cd ../dots-wechat-bridge-reviewed
python3 -B scripts/scan-public-bundle.py . --json
```

扫描器针对无 Git 历史、无运行状态的发行目录；直接扫描克隆目录会正确拒绝 `.git`。按 [离线验证说明](docs/VALIDATION.md) 核对 Manifest 的全部字节和 SHA-256 后再安装。代码复制或后续发布的 commit 必须独立验证。

1. 在上述独立的、路径合规的目录确认 Python 3.10+。若系统默认 `python3` 较旧，指定已安装的解释器：`PYTHON_BIN=/absolute/path/to/python3.11 sh scripts/bootstrap.sh`；否则运行 `sh scripts/bootstrap.sh`。脚本创建新的 `.venv`，安装锁定依赖、跑离线测试和 schema 检查；已有 `.venv` 会拒绝覆盖。只在新安装目录使用，不对正在运行的实例执行。
2. 可选地按 [Skill 安装说明](docs/SKILL-INSTALLATION.md) 先预览再安装。把本候选源码路径和当前任务交给 Codex；Skill 不提供插件、订阅、权限或密钥。
3. 让 Codex 先只读核查主机、已有实例、产品 Tunnel/插件/MCP Events 能力和目标 Dot，再按下方六个关卡报告各自证据。本人在私有终端处理专用密钥、扫码和手机确认。既有实例走 [升级与恢复](docs/RELIABILITY-DEPLOYMENT.md)，保留原订阅、绑定、状态和固定 ACK 设置。安装 Skill、安装依赖、连接插件、绑定微信和订阅事件是不同关卡；每关都要读回真实结果。

不使用 Codex 时，按下方第 1–6 节人工执行；每一步的“通过标准”都是独立检查项。断网、重启、未知发送、暂停、卸载和兼容回退见 [运维说明](docs/OPERATIONS.md)。干净目录离线测试通过后仍须分别核实实际 MCP 调用、唯一订阅、精确目标完整读回和本人手机收件；`accepted` 只表示接口接受。

## 配置前检查

- 运行主机：Linux（systemd 服务路径）或 macOS（开发/前台运行），Python 3.10+，可以出站访问微信、腾讯 CDN 和 OpenAI。Windows 原生当前不支持 `fcntl`；使用 Linux 主机。
- 2 核/2GB 主机足以承载本桥接的小样本，不在服务器安装模型或 ASR。
- AI 环境：能使用外部 MCP 工具；持续接收来信还需要产品提供 **MCP Events** 订阅和相应执行能力。普通的 MCP 工具调用与事件后台执行是不同能力。
- OpenAI：账号/工作区实际提供 Secure MCP Tunnel 和开发者插件入口，以及管理隧道和使用隧道的权限。功能可见性受产品、账号、工作区和管理员策略影响；缺入口时停止这一分支，核对官方文档/管理员，不伪造配置成功。
- 微信：由本人扫描并确认的 iLink 机器人绑定，不需要将个人微信密码交给本仓库。

平台权限和支持范围请以 [Secure MCP Tunnel 官方指南](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 和 [tunnel-client 入门文档](https://github.com/openai/tunnel-client/blob/master/docs/onboarding.md) 为准。[MCP Events 官方文档](https://developers.openai.com/plugins/build/mcp-events) 要求协议版本 `2026-07-28`。页面和产品入口会更新。

## 1. 取得源码，先跑离线测试

在选定运行主机操作；示例安装位置为 `/opt/dots-wechat-bridge`。使用已有部署时按 [运维文档](docs/OPERATIONS.md) 更新，初次安装脚本不会覆盖已有服务。

```sh
git clone https://github.com/heyxiafei/dots-wechat-bridge.git
cd dots-wechat-bridge
sh scripts/bootstrap.sh
```

通过标准：依赖安装成功、全部离线测试通过、`scripts/generate-schema.py --check` 通过。测试使用临时数据库和合成样本，不访问真实账号或发送消息。

Linux 长期服务需要把干净源码放在最终安装目录，再在该目录创建 `.venv`，不要移动已创建的虚拟环境。确保专用服务用户能读源码和执行虚拟环境。macOS 开发运行可保留当前工作目录。

## 2. 本人设置 Platform 隧道与密钥

1. 在 [Platform Tunnels](https://platform.openai.com/settings/organization/tunnels) 创建或选择专用私有隧道。关联实际调用方的 Platform 组织和 ChatGPT 工作区，保存返回的 tunnel ID。
2. 管理者需要 Tunnels Read + Manage；长期运行者使用专用 runtime key 的 Tunnels Read + Use。根据账号表单选择合适期限，记录到期时间。运行进程不使用 Admin key。
3. 从上述页面或 [官方 Releases](https://github.com/openai/tunnel-client/releases/latest) 下载匹配系统/架构的官方 `tunnel-client`。保留发布包内需要的配套文件，放在安装目录 `bin/`，确认可执行。仓库不打包第三方二进制。
4. 本人运行下面脚本，在终端隐藏输入密钥。助手使用文件引用，不读取或复制密钥值。

```sh
python3 scripts/set-runtime-key.py
bin/tunnel-client help quickstart
```

通过标准：官方客户端可以执行；私有 `runtime/secrets/runtime.key` 已由本人创建，权限600。密钥、二维码、运行配置和数据库属于 `runtime/`，已被 Git 排除。

## 3. 生成私有配置并连上隧道

首次还不知道 callback hostname 时，使用真实 tunnel ID 和显式的发现模式。此模式的回调允许列表为空，**拒绝全部回调且不建立订阅**，但允许连接 MCP 工具；第5步再从本人授权的订阅请求中核实精确主机。下面 tunnel ID 占位符必须替换。

```sh
.venv/bin/python scripts/render-config.py \
  --tunnel-id '<YOUR_TUNNEL_ID>' \
  --discover-callback
bin/tunnel-client doctor --profile-file runtime/profile.yaml --explain
bin/tunnel-client run --profile-file runtime/profile.yaml
```

生成器引用当前安装目录的 Python、源码、状态和密钥文件；服务健康地址仅监听 `127.0.0.1` 动态端口。配置不需要新增公网入站端口、修改代理或关闭防火墙。

若已从本人授权的平台订阅元数据核实精确 callback hostname，可用 `--callback-host '<EXACT_PLATFORM_CALLBACK_HOST>'` 替代 `--discover-callback`，最多重复3次。不能用 `*` 或放开全部互联网域名，也不能自动信任未经核实的请求主机。

通过标准：doctor 检查通过、`/healthz` 和 `/readyz` 返回200。`runtime/state/service-health.url` 存放私有环回地址。此时只证明隧道和 MCP 存活，尚未绑定微信。

## 4. 连接插件与本人微信

在支持的 ChatGPT/AI 产品中创建私有开发者 MCP 插件，选用已关联的 Tunnel。使用产品公开提供的设置界面与权限，保持服务器运行。调用 `get_weixin_status`，确认工具实际可调用。完整 schema 在 [docs/mcp-schema.json](docs/mcp-schema.json)。

Linux 长期运行先在第3步运行隧道的终端按 **Ctrl-C**，等待 `tunnel-client` 与子 worker 完全退出，再审阅生成的 unit 并安装。不要同时启动前台和 systemd 实例；安装器会在更改文件归属或 unit 前检查 worker 锁，发现活动 worker 时退出。

```sh
sudo sh scripts/install-service.sh
sudo systemctl stop dots-wechat-bridge.service
sudo -u dotsbridge .venv/bin/python probe/login.py --state-dir runtime/state --start
sudo -u dotsbridge .venv/bin/python scripts/render-qr.py --state-dir runtime/state
```

本人私下查看生成的 `runtime/state/weixin-login-qr.png`，用微信扫码并在手机确认；只在私有本地视图展示。随后轮询：

```sh
sudo -u dotsbridge .venv/bin/python probe/login.py --state-dir runtime/state --poll
```

在 `--poll` 返回 `confirmed` 前保持原 worker 停止；若仍等待扫码，稍后只运行 `--poll`。确认绑定后才运行 `sudo systemctl start dots-wechat-bridge.service`。登录脚本会在打开数据库前检查原 `worker.lock`，运行中返回 `worker_running_stop_before_login`。如果微信要求额外校验码，由本人按提示在私有终端处理 `--verify-stdin`；不能让助手从聊天读取验证码。超时需重新开始二维码流程。绑定者身份变化会被拒绝，不能用更换二维码静默切换为另一个收件人。

macOS 前台运行：先停止原 `tunnel-client run` 并等待 worker 退出，再使用相同登录命令去掉 `sudo -u dotsbridge`；绑定确认后重新启动原前台隧道。macOS 前台运行没有 systemd 的 CPU/内存限额，仍保留文件、块、缓存和队列上限。Linux 已运行 systemd 后不要再启动第二个 foreground/runtime 实例。

通过标准：QR 轮询 `confirmed`；随后 `get_weixin_status` 返回 `bound=true / owner_only=true / account_status=ready`，收到微信消息后 `connected=true`、`last_error=null`。这证明绑定和轮询，没有证明 AI 事件订阅已经工作。

## 5. 为微信来信创建一个事件订阅

让支持 MCP Events 的调用方通过公开的 `events/list` 找到 `weixin.message`，以 arguments `{}` 创建并维护**一个**订阅。公开协议使用 `events/subscribe` / `events/unsubscribe`，callback URL 和签名由调用方提供。本桥接验证 HTTPS callback、公共 DNS、精确主机允许列表并持久保存订阅。

第3步若使用了发现模式，先让本人授权的调用方尝试一次 `weixin.message` 订阅。预期返回 `invalid_destination`，不会发送网络回调或留下有效订阅。调用 `get_probe_status`（arguments `{}`），从 `recent_rpc` 的 `events/subscribe / received` 记录读取 `callback_host`；该工具不需要创建 `test.ping` 订阅。诊断仅有 hostname/错误类别，不含完整 URL 或签名。将主机与平台订阅元数据或管理员确认的信息核对；来源无法确认就停止订阅配置，保持拒绝全部回调。

核实后，Linux 在最终安装目录执行以下命令，保留原 key、state 与 QR 绑定：

```sh
sudo systemctl stop dots-wechat-bridge.service
sudo -u dotsbridge .venv/bin/python scripts/render-config.py \
  --tunnel-id '<YOUR_TUNNEL_ID>' \
  --callback-host '<VERIFIED_EXACT_PLATFORM_CALLBACK_HOST>' --replace
sudo systemctl start dots-wechat-bridge.service
```

macOS 先按 Ctrl-C 并等待前台隧道退出，用同一 `render-config.py` 命令去掉 `sudo -u dotsbridge`，随后重启第3步的前台隧道。重新验证 health/ready 与 MCP 工具，再让原调用方重试原 `weixin.message` 订阅。确切主机允许列表仍受 HTTPS、公共 DNS 和地址校验约束。不要把 callback URL、签名密钥或完整日志贴到公开聊天/Issue。

收到 `weixin.message` 的 message_id 后，助手遵循 [回复工作流](docs/REPLY-WORKFLOW.md)：先读取核验，再按复杂度选择单条 final 或 ack → final 回本人微信。需要实际附件时先发贴合任务的 ack，再读取内容并完成任务。此次部署的旧 dot 镜像保持只读。订阅到期需要由原调用方刷新；服务器在线本身不会永久维持调用方任务。

旧 `mirror-journal-v1` 记录只作历史对账，背景契约见[逐部分镜像契约](docs/DOT-MIRROR-JOURNAL.md)。保留只读模式时不准备、授权、提交或补写镜像；未知结果不重发。

已有部署升级到0.5.0时，先验证旧工具可调用，再在原插件 **Refresh tools**，确认 `reply_weixin_message_phase` 可用后更新原事件处理提示。不另建连接或订阅。旧 `reply_weixin_message` 是单条 final 接口；它与新工具的 final 共用同一幂等槽。新工具的 `phase:"final"` 要求先承诺 ack，不能拿它替代简单对话的直接 final。

产品若没有事件订阅/后台执行能力，停止持续来信自动处理的配置，说明能力缺口；可以保留手动调用工具的能力。不要把普通定时轮询冒称事件订阅，也不要为连通性创建额外 `test.ping` 订阅。代码保留的合成探针仅供离线协议测试。

通过标准：真实 owner 来信产生事件，原文和稳定 message_id 可核对；简单请求只有一条完整微信回答，需要处理时间的任务有 ack 与 final 两条完整回复。工具状态与手机收件确认分别记录，不承诺零延迟或原生右侧用户气泡。

## 6. 验收图片、文件和语音

先明确授权一张无敏感小 PNG 和一个 TXT 发送给本人。助手从自己执行环境读取实际文件，按照 [媒体工具契约](docs/MEDIA.md) 完成 `begin → chunk → finalize → send → status`。用稳定 ID 重试，不制造重复测试消息。

入站文件需分块重组并核对 whole SHA256；图片通过 MCP image block 查看实际像素。语音只用微信提供的转写；上游未提供时说明缺失，请本人发文字。

发送接口 `accepted` 表示腾讯 API 接受，`delivered=null`；只有本人报告收到，才能附加“本人已确认收到”的证据。网络错误/发送超时可能已经发送，状态为 `uncertain`，不得更换 ID 盲重发。

本发布的离线验证不证明任何账号的真实文字、图片、文件、语音转写或出站 PNG/TXT 已验收；部署后需逐项验证。合成离线资源检查可运行：

```sh
.venv/bin/python scripts/outbound-resource-check.py
.venv/bin/python scripts/media-resource-check.py
sudo -u dotsbridge .venv/bin/python scripts/safe-status.py --state-dir runtime/state
systemctl show dots-wechat-bridge.service --property=MemoryCurrent,MemoryMax,CPUQuotaPerSecUSec,TasksCurrent
```

最后两条针对已安装的 Linux 服务；macOS 的状态命令去掉 `sudo -u dotsbridge`，不运行 systemctl。

验收结束应检查服务 cgroup 的 `memory.peak` 和 `memory.events`，记录测量样本、时间范围及 OOM 计数；小样本通过不能宣称已完成20MiB压力测试。

## 资源边界与维护

单文件20MiB，块64KiB，入/出站共享缓存128MiB/7天；下载并发1、出站上传并发1、未承诺上传8、待发队列32，重试最多3次。Linux 模板 systemd 服务最多使用1核、内存384MiB；macOS 前台没有这些系统级限额。图片800万像素/预览最长边2048。上传、整文件 hash 和加密走小块，不把整文件 base64 塞进对话。原入站解密使用20MiB有界缓冲。

本版没有可推广到生产负载的资源测量承诺；应在获准的实际主机上记录资源峰值及 OOM 计数。数据库消息历史持久保留，7天TTL针对媒体缓存；按自己的保留策略管理私有历史和备份。

更新、回滚、上下文失效、认证到期、密钥轮换见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。核心代码、测试和资源边界不依赖某个私人服务器或聊天记录。

## 来源与许可

微信媒体协议参考 [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin)，相关 MIT notice 保留在 [NOTICE.md](NOTICE.md) 与 `licenses/`。OpenAI tunnel-client 单独从官方来源安装，遵循其许可证。本仓库不包含第三方二进制或官方文档整份副本。

原创桥接代码和文档采用 [MIT 许可证](LICENSE)，允许使用、修改和分发，保留版权及许可声明。第三方依赖及协议参考保留各自许可证，见 [NOTICE.md](NOTICE.md)。
