# dots-wechat-bridge

> **singletrial1 + steadyfuture1：独立公开候选源码与可安装 Skill。** 运行版本以 `probe/probe.py` 的 `BUILD_VERSION` 为准；发布前以本包的 manifest、当前离线验证和准确 commit/tag 核对实际能力。候选保留持久关闭、从未授权 prepared 恢复、来源完整性清单、入站失败记录及一次性单来源试验，并包含单条试验关闭后按代际只接纳未来首次入库来源的有限持续准入能力；安装或试验本身不会启用它。源码包的离线结果不证明你的主 Dot 读回、手机送达或真实持续同步已验收。见 [单条试验契约](docs/SINGLE-SOURCE-TRIAL.md)、[持续来源准入](docs/STEADY-FUTURE-ADMISSION.md) 和 [部署/回退范围](docs/RELIABILITY-DEPLOYMENT.md)。
>
> 安装 Skill 只增加助手工作指南，不会安装或连接私人 MCP 插件、开通准入、创建凭据或发送消息。见 [安装 Skill](docs/SKILL-INSTALLATION.md)。公开包不含任何用户的配置、消息、数据库或依赖二进制。
>
> 普通微信收发与 Dot 镜像独立。新来源首次镜像不依赖整段无关聊天历史；已授权或 unknown 必须保留并正向对账，不能重发。`accepted` 不代表手机送达；离线测试不证明主 Dot 工具可见、严格恰好一次或 3 秒响应。Dot 的原生出站自动导出尚未证实。

把自己的微信接到支持 MCP 的 AI 助手。服务器负责收发和私有媒体传输，AI 在自己的产品环境里理解内容、生成回答。服务器不运行模型；平台 webhook 唤起 dot 推理、读取消息和调用转发工具仍会消耗模型 token，不能把整条 AI 链路称为零 token。

这是独立社区项目，未获 OpenAI 或腾讯背书。`dot/dots` 在这里指用户正在使用的助手；仓库没有调用产品内部编排、记忆或隐藏接口。

上游基线版本为 **0.5.0**：简单对话一条完整回复，需要研究、读取附件或执行任务时采用 ack → final 两阶段。回复分支由事件执行方选择，服务器负责固定槽位、顺序和幂等；这与默认关闭的固定 system ACK 是两项设置。

## Codex 快速开始

先取得经过审阅的**本候选源码包**，确认 `PUBLIC-BUNDLE-MANIFEST.json` 中的文件哈希和 [发布检查](docs/PUBLISHING.md)。[heyxiafei/dots-wechat-bridge](https://github.com/heyxiafei/dots-wechat-bridge) 是本人的独立公开仓库；仓库存在不证明本候选已上传，须以该仓库实际发布 commit/tag 与 Manifest 读回确认。[RerrentLinden/dots-wechat-bridge](https://github.com/RerrentLinden/dots-wechat-bridge) 仅是 MIT 上游来源；不要与 `dot-blender-studio` 混用。

1. 在独立目录解压候选，确认 Python 3.10+。若系统默认 `python3` 较旧，指定已安装的解释器：`PYTHON_BIN=/absolute/path/to/python3.11 sh scripts/bootstrap.sh`；否则运行 `sh scripts/bootstrap.sh`。脚本创建新的 `.venv`，安装锁定依赖、跑离线测试和 schema 检查；已有 `.venv` 会拒绝覆盖。只在新安装目录使用，不对正在运行的实例执行。
2. 可选地按 [Skill 安装说明](docs/SKILL-INSTALLATION.md) 先预览再安装。把本候选源码路径和当前任务交给 Codex；Skill 不提供插件、订阅、权限或密钥。
3. 让 Codex 先只读核查主机、已有实例、产品 Tunnel/插件/MCP Events 能力和目标 Dot，再按下方六个关卡报告各自证据。本人在私有终端处理专用密钥、扫码和手机确认。既有实例走 [升级与恢复](docs/RELIABILITY-DEPLOYMENT.md)，保留原订阅、绑定、状态和固定 ACK 设置。安装 Skill、安装依赖、连接插件、绑定微信和订阅事件是不同关卡；每关都要读回真实结果。

不使用 Codex 时，按下方第 1–6 节人工执行；每一步的“通过标准”都是独立检查项。断网、重启、未知发送、暂停、卸载和兼容回退见 [运维说明](docs/OPERATIONS.md)。干净目录离线测试通过后仍须分别核实实际 MCP 调用、唯一订阅、精确目标完整读回和本人手机收件；`accepted` 只表示接口接受。

## 把本候选交给你的 dot

下方使用已核实的本人独立公开仓库地址。使用远端代码前，先固定包含本候选的实际 commit/tag，并核对 Manifest；远端尚无对应版本时，直接提供经审阅的本源码包：

> 请根据 https://github.com/heyxiafei/dots-wechat-bridge 中已核实的具体发布 commit/tag，或本源码包的 README 和 AGENTS.md，帮我在我授权的主机上配置只与本人微信收发的桥接。先检查本包的版本与最新验证，再检查系统、执行能力、Secure MCP Tunnel、插件和 MCP Events 是否可用，按验证关卡推进；离线通过不等于我的账号已真实闭环。我亲自创建和输入凭据、扫描二维码。微信来信先读取核验，Dot 原文镜像按逐消息 journal 与单条试验契约独立处理；不要依赖无关历史窗口，也不要重新发送已授权或 unknown。镜像无法验收时，不阻塞已核验本人消息的幂等微信回复。简单聊天或立即可答的问题，用 reply_weixin_message 直接发送一条完整 final，并在 Dot 单独展示同一回答；需要研究、读取附件或执行的任务，先用 reply_weixin_message_phase 发送简短 ack，完成后发送一条完整 final，两条都在 Dot 单独展示。根据 reply_state 恢复已承诺阶段，避免重复发送；不要把原文和回答合并。普通 Dot 聊天仅在我明确要求时显式调用工具发送微信。首次镜像试验只用一条唯一无附件短文，完成后关闭试验准入；图片和 TXT 属于之后另行批准的媒体验收，报告实际发送状态与资源峰值。

AI 可以检查环境、安装依赖、生成配置、测试和部署。**本人仍需授权目标主机、处理账号权限、创建/私下输入运行密钥、微信扫码并确认收到测试文件。** 只有仓库读取能力、没有终端/SSH/浏览器的助手会给你可执行步骤，不能代替执行或宣称部署成功。

## 功能

| 方向 | 支持内容 |
|---|---|
| 微信 → AI | 精确文字、图片、文件，私有附件 ID、分块读取、图片预览 |
| 微信语音 → AI | 微信上游已经提供的文字转写；没有转写时提示改发文字 |
| AI → 微信 | 简单请求一条文字回答、复杂任务 ack → final；用户明确要求的通知、图片、文件 |
| 视频入/出站 | 原生 MP4 视频与把 MP4 作为普通文件传输是不同契约；单个文件上限20MiB，原生视频不解码或转码，手机收取和原生播放仍需本人分别验收 |
| 身份 | 只接受二维码绑定者的私聊，只向该绑定者发送 |
| 状态 | 持久队列、稳定 ID 去重、API 接受/失败/不确定状态 |

微信侧的 owner-only 限制的是收发对象。MCP 侧隐私依赖 **Tunnel 和插件的访问控制**：桥接没有逐调用者身份认证，任何获准调用该隧道工具的人都可能读取绑定者消息、请求向绑定者发送。使用仅本人可访问的专用私有隧道与插件，核对组织/工作区授权，不向其他账号共享工具访问权限。

图片/文件/视频出站需要把**实际字节**经认证 MCP 分块传到桥接服务器，之后流式加密上传腾讯 CDN。原生视频与普通文件不可混用上传票据或在不确定结果后自动切换补发，详见 [媒体契约](docs/MEDIA.md)。本候选的离线协议测试不等于真实手机验收。另一个机器上的路径或无法访问的 URL 不能代替文件内容。微信群、多用户收发、原音频识别、自动转发全部聊天不在当前范围。

## 配置前检查

- 运行主机：Linux（systemd 服务路径）或 macOS（开发/前台运行），Python 3.10+，可以出站访问微信、腾讯 CDN 和 OpenAI。Windows 原生当前不支持 `fcntl`；使用 Linux 主机。
- 上游以 2 核/2GB 主机承载其小样本；本候选未重新完成资源验收。不在服务器安装模型或 ASR。
- AI 环境：能使用外部 MCP 工具；持续接收来信还需要产品提供 **MCP Events** 订阅和相应执行能力。普通的 MCP 工具调用与事件后台执行是不同能力。
- OpenAI：账号/工作区实际提供 Secure MCP Tunnel 和开发者插件入口，以及管理隧道和使用隧道的权限。功能可见性受产品、账号、工作区和管理员策略影响；缺入口时停止这一分支，核对官方文档/管理员，不伪造配置成功。
- 微信：由本人扫描并确认的 iLink 机器人绑定，不需要将个人微信密码交给本仓库。

平台权限和支持范围请以 [Secure MCP Tunnel 官方指南](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 和 [tunnel-client 入门文档](https://github.com/openai/tunnel-client/blob/master/docs/onboarding.md) 为准。[MCP Events 官方文档](https://developers.openai.com/plugins/build/mcp-events) 要求协议版本 `2026-07-28`。页面和产品入口会更新。

## 1. 取得源码，先跑离线测试

以下是新实例的操作说明，不能作为现有实例的升级命令。先按 [公开包说明](docs/PUBLISHING.md) 审阅、冻结和离线验收；真实平台操作仍需相应授权。在选定运行主机操作，示例安装位置为 `/opt/dots-wechat-bridge`。使用已有部署时按 [当前部署与回退计划](docs/RELIABILITY-DEPLOYMENT.md) 更新，初次安装脚本不会覆盖已有服务。

```sh
cd '<REVIEWED_CANDIDATE_SOURCE_DIR>'
sh scripts/bootstrap.sh
```

如默认 `python3` 不满足 3.10+，用 `PYTHON_BIN=/absolute/path/to/python3.11 sh scripts/bootstrap.sh`。通过标准：依赖安装成功、全部离线测试通过且无 skip、`scripts/generate-schema.py --check` 通过。测试使用临时数据库和合成样本，不访问真实账号或发送消息。版本、执行日期和结果应在本次安装记录，不能引用历史计数冒充新验证。

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

以下扫码步骤只用于获准的新实例。已有实例先只读核原绑定和插件，不能为升级再次扫码、建立第二个连接或静默换 owner。

在支持的 ChatGPT/AI 产品中创建私有开发者 MCP 插件，选用已关联的 Tunnel。使用产品公开提供的设置界面与权限，保持服务器运行。调用 `get_weixin_status`，确认工具实际可调用。完整 schema 在 [docs/mcp-schema.json](docs/mcp-schema.json)。

Linux 长期运行先在第3步运行隧道的终端按 **Ctrl-C**，等待 `tunnel-client` 与子 worker 完全退出，再审阅生成的 unit 并安装。不要同时启动前台和 systemd 实例；安装器会在更改文件归属或 unit 前检查 worker 锁，发现活动 worker 时退出。

```sh
sudo sh scripts/install-service.sh
sudo -u dotsbridge .venv/bin/python probe/login.py --state-dir runtime/state --start
sudo -u dotsbridge .venv/bin/python scripts/render-qr.py --state-dir runtime/state
```

本人私下查看生成的 `runtime/state/weixin-login-qr.png`，用微信扫码并在手机确认；只在私有本地视图展示。随后轮询：

```sh
sudo -u dotsbridge .venv/bin/python probe/login.py --state-dir runtime/state --poll
```

如果微信要求额外校验码，由本人按提示在私有终端处理 `--verify-stdin`；不能让助手从聊天读取验证码。超时需重新开始二维码流程。绑定者身份变化会被拒绝，不能用更换二维码静默切换为另一个收件人。

macOS 前台运行：在另一个终端使用相同命令，去掉 `sudo -u dotsbridge`；保留 `tunnel-client run` 进程。macOS 前台运行没有 systemd 的 CPU/内存限额，仍保留文件、块、缓存和队列上限。Linux 已运行 systemd 后不要再启动第二个 foreground/runtime 实例。

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

收到 `weixin.message` 的 message_id 后，助手遵循 [回复工作流](docs/REPLY-WORKFLOW.md)：先读取核验，Dot 原文镜像按当前 journal 与单来源契约独立处理，新来源首次许可只核对自身清单，微信回复按复杂度选择单条 final 或 ack → final。旧来源的历史 `partial`、自身内容缺失或 unknown 只暂停对应镜像，不阻塞已核验本人消息的授权幂等微信回复。需要实际附件时先发简短 ack，再读取内容并完成任务；各部分的 Dot 展示与微信回复分别核验，不能以其中一边成功推断另一边成功。桥接不提供跨产品事务，镜像去重和失败恢复由调用方记录。订阅到期需要由调用方刷新；服务器在线本身不会永久维持调用方任务。journal 回执只提交 [准确的单键元数据投影](docs/DOT-MIRROR-JOURNAL.md#回执证据的准确包装)，不要把 Dot 消息额外的 `weixin_mirror` 塞进 `evidence.message_metadata`。

Dot 中的发言标签由实际调用方展示，不是桥接服务器的身份或同步开关。可按安装者偏好为**新展示**配置示例标签，如 `🙂 我（微信）`、仅在微信确实提供上游转写时用 `🙂 我（微信语音转写）`，以及 `🤖 助手`。这些只是可替换的示例名称与表情；不能把标签变化当成旧消息重放许可，也不会替旧消息改名或自动打开镜像准入。

已有部署从上游 0.5.0 升级本候选时，先按 [部署计划](docs/RELIABILITY-DEPLOYMENT.md) 核旧工具和关闭状态，再在原插件 **Refresh tools** 并由目标主 Dot 验证新 schema 的只读工具，最后审阅原事件提示的必要改动。不另建连接或订阅。旧 `reply_weixin_message` 是单条 final 接口；它与新工具的 final 共用同一幂等槽。新工具的 `phase:"final"` 要求先承诺 ack，不能拿它替代简单对话的直接 final。

产品若没有事件订阅/后台执行能力，停止持续来信自动处理的配置，说明能力缺口；可以保留手动调用工具的能力。不要把普通定时轮询冒称事件订阅，也不要为连通性创建额外 `test.ping` 订阅。代码保留的合成探针仅供离线协议测试。

通过标准：真实 owner 来信产生事件，原文在 Dot 单独展示；简单请求只有一条完整回答，需要处理时间的任务有 ack 与 final 两条完整回复，各自与 Dot 文字一致。逐个读回期望的原文、回复及附件，记录 `partial=false` 与准确目标元数据；单条文字试验不能代替附件或持续多消息验收。工具状态与手机收件确认分别记录，不承诺零延迟、原生右侧用户气泡或所有原生 Dot 对话自动双向同步。

一处私人部署曾完成**一条无附件短文**的限定验收：Dot 中原文与最终回复分别精确读回；本人手机截图显示这条测试原文和对应回复各一条，截图可见范围无重复。这里只保留脱敏结论，不包含截图、账号、消息 ID 或对话正文。这份历史证据不能证明本候选在新用户环境已部署，也不覆盖全历史、附件、未知发送恢复、断线重启、订阅续期或持续多消息准入。

## 6. 验收图片、文件和语音

先明确授权一张无敏感小 PNG 和一个 TXT 发送给本人。助手从自己执行环境读取实际文件，按照 [媒体工具契约](docs/MEDIA.md) 完成 `begin → chunk → finalize → send → status`。用稳定 ID 重试，不制造重复测试消息。

入站文件需分块重组并核对 whole SHA256；图片通过 MCP image block 查看实际像素。语音只用微信提供的转写；上游未提供时说明缺失，请本人发文字。

发送接口 `accepted` 表示腾讯 API 接受，`delivered=null`；只有本人报告收到，才能附加“本人已确认收到”的证据。网络错误/发送超时可能已经发送，状态为 `uncertain`，不得更换 ID 盲重发。

上游 README 声称其一次私有部署完成了真实文字、图片、文件、上游语音转写及出站 PNG/TXT 验收。这是上游记录，本公开候选尚未复现这组验收，也不保证不同账号/产品具备相同平台权限。合成离线资源检查可运行：

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

上游 README 记录其私有单次出站小样本的服务 cgroup 峰值约35.1MiB、OOM计数0，以及离线2MiB文件+小PNG出站峰值RSS约37MiB。这些是上游样本声称，不是本候选的验收或压力测试结果。数据库消息历史持久保留，7天TTL针对媒体缓存；按自己的保留策略管理私有历史和备份。

更新、回滚、上下文失效、认证到期、密钥轮换见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。核心代码、测试和资源边界不依赖某个私人服务器或聊天记录。

## 来源与许可

本候选基于 MIT 上游 [RerrentLinden/dots-wechat-bridge v0.5.0](https://github.com/RerrentLinden/dots-wechat-bridge)；该链接只用于来源与许可归属，不是本人的发布地址。获取本候选请使用 [本人独立公开仓库](https://github.com/heyxiafei/dots-wechat-bridge) 中经核验与本包一致的具体 commit/tag，或本源码包。微信媒体协议参考 [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin)，事件调度、租约和时间记录思路静态参考 [Lawrence/wx-bot](https://github.com/lawrencewzen/wx-bot)。具体来源范围与 MIT notice 保留在 [NOTICE.md](NOTICE.md) 与 `licenses/`。OpenAI tunnel-client 单独从官方来源安装，遵循其许可证。本仓库不包含第三方二进制或官方文档整份副本。

上游桥接代码和文档采用 [MIT 许可证](LICENSE)，Copyright 2026 RerrentLinden，允许使用、修改和分发并保留版权及许可声明；本候选保留原 LICENSE。第三方依赖及参考来源保留各自许可证，见 [NOTICE.md](NOTICE.md)。

当前分发验证范围与可复现命令见 [离线验证说明](docs/VALIDATION.md)。
