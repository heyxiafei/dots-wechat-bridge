# 安装项目并由本人扫码

目标体验是：dot 完成获准的安装与平台接入准备，本人只在私有本机窗口扫码并在微信确认，安装器核验接续门槛后启动服务。首次平台授权、运行密钥、插件接入及新旧收发切换不能用扫码代替。本项目不承诺未经这些授权即可零配置接入。

## 可复现安装与离线启动

使用 Python 3.10+；系统默认 Python 版本不足时，指定已安装且满足要求的解释器。安装到尚不存在的目录：

```sh
python3.11 scripts/install-project.py install --prefix /absolute/new/project --tests
```

已有匹配平台和 Python 的依赖 wheel 时，可加 `--wheelhouse /absolute/wheels` 完全离线安装。安装器只复制项目源码/文档/测试，不复制旧 runtime、认证、消息数据库或私有配置；独立创建 `.venv`，按 requirements.lock 安装依赖，验证 schema，实际启动离线 stdio 进程。现有目的目录一律拒绝覆盖。失败目录保留供诊断，不自动清理或重试。

离线进程使用 `runtime/install-test-state`，与未来生产 `runtime/state` 分开；标记的测试库不会被提升为生产库。离线模式不启用 polling、sender、媒体或回调线程，禁用网络，只允许协议发现和两个状态读取工具，发送及订阅请求明确拒绝。完整测试中的模拟账号/发送不会写入生产库。可以重跑：

```sh
/absolute/new/project/.venv/bin/python /absolute/new/project/scripts/install-project.py check --prefix /absolute/new/project --tests
```

## 首次接入：由安装方准备，必须有具体批准

1. 核实实际平台支持的 Tunnel、私有插件和 MCP Events；按当前官方界面核权限，取得真实隧道 ID，不填造 ID。运行密钥由本人在本机隐藏输入，只给该服务必要的 Tunnels Read/Use；助手不得读取、搬运现有认证或让用户把密钥贴到聊天。
2. 从官方来源取得匹配平台的 tunnel-client，核验版本与发布校验值，放到该安装目录 `bin/`。本项目不附带该二进制。使用原 `render-config.py` 生成引用本目录密钥的 profile；先拒绝全部回调，再依据获准平台请求核实精确 callback host。禁止修改防火墙、代理或放宽网络域名作为捷径。
3. 安装方记录本次用户批准、profile 与已核客户端哈希及单写者隔离证据到私有 `runtime/setup-approval.json`，结构见 example。默认模板全为 null，不能直接启动。此记录只是防误启动的本地条件，不授予平台权限，也不能替代真实批准/旧写入者停止证据。
4. `isolation_mode` 仅可为 `no_existing_writer_verified`（真实首次安装、已核没有旧写入者）或 `old_writer_fenced`（已有服务切流，记录旧写入者停止接收/发送的真实证据）。旧服务仍在线时，不得使用“首次安装”或因为新数据库为空而放行。仅不同目录或不同 bot 不等于已隔离。
5. `startup_mode` 可以是 `foreground` 或经明确批准的 `launch_agent`。后者在 macOS 当前用户 LaunchAgents 下注册仅本安装目录对应的独立 label，登录时启动、异常退出后受节流地重启。不得覆盖已有 plist 或重试结果未知的 bootstrap。注册成功不等于 MCP ready，更不等于手机收到。

上述准备完成后，安装方运行：

```sh
/absolute/new/project/.venv/bin/python /absolute/new/project/scripts/install-project.py connect --prefix /absolute/new/project
```

安装器重新核验源码、依赖、profile/客户端哈希、隔离批准与私有密钥文件元数据；不读取密钥内容。本人扫码图仅在本机 Preview 展示；最多等待 5 分钟，确认绑定后才启动。过期、额外验证、未知或超时均停止，不自动重建二维码/重试，不把 confirmed 当成收件回执。微信若要求额外验证，需要本人处理，不能保证只扫码即可完成这种账号分支。

### 旧服务接续的强制启动门槛

`old_writer_fenced` 不能凭新库中的一条 ready 绑定启动。`runtime/setup-approval.json` 还须有 `handoff` 对象，且每次真正启动（包括 LaunchAgent 重启）核验以下本地事实：

- `continuity_mode` 必须为 `retained_state`。本版安装器不实现只接收的维护服务，也不支持 `new_source_cutpoint`；缺原状态时保持离线，不能编造新消息切点。
- `owner_binding_sha256` 必须匹配实际数据库唯一的 `[bot_id, owner_id]` JSON（紧凑分隔符，默认 ASCII 转义）的 SHA256；只比较指纹，不输出身份或读取凭据列。
- `subscription_id` 必须匹配保留的已验证 `weixin.message` 原记录，且不存在不同 ID 的当前有效微信订阅。原记录可仍有效或自然 TTL 到期；缺记录、明确撤销（`expires=0`）仍阻止启动。其他已过期历史记录原样保留，不视为当前竞争者，也不获准恢复。
- `platform_route_evidence_reference`、`subscription_continuity_evidence_reference`、`history_retention_evidence_reference` 必须分别引用已审阅的外部真实证据。非空字符串和哈希不能证明旧实例已停止、平台路由正确或订阅已接续；安装方仍须核实这些事实，禁止从新空库生成“保留历史”的证明。
- `history.weixin_inbox` 与 `history.weixin_outbox` 各有 `through_rowid`、`count`、`sha256`。检查点来自真实旧实例停止后的一致 SQLite 快照，不含凭据。inbox 历史必须非空；outbox 可以为空。检查点覆盖截止 rowid 以内的原始身份和已承诺内容，后续新增行、发送状态和订阅到期变化不改变它。
- outbox 检查点还包含 `submission_states`，记录同一快照中每条原行的 `[rowid,status]`。原 queued/preparing/auth/context 状态可正常推进；原 accepted/rejected/unknown/cancelled/superseded 必须保留终态，原 sending 只可保留或由原恢复逻辑转为 unknown，不能在迁移时变回 queued。此表只用于核对原状态，不授权重试或修改记录。

历史 SHA256 的输入为按 rowid 升序查询的每行 JSON，再追加一个换行；使用 `ensure_ascii=True, separators=(',', ':')`。inbox 列顺序是 `rowid,id,account,peer,source_id,text,created_at,kind`；outbox 列顺序是 `rowid,message_id,account,peer,text,client_id,created_at,kind,upload_id,source_message_id,ack_origin`。只输出摘要和计数，保留原数据库与消息内容私有。安装器不导入或迁移任何凭据、队列、订阅，不为这个检查自动读取旧 runtime。

扫码入口完成后先独立读取绑定状态；绑定未能核实则返回 `binding_unconfirmed`，不声称 confirmed。绑定已核实而接续门槛未通过，或扫码期间 profile/密钥元数据等发生变化时，`connect` 返回 `status=bound_but_not_started`、`production_started=false` 和固定错误类别，保留现有绑定。此时应核对接续证据和状态，不能把它当成二维码失败而重复扫码。真实首次安装的 `no_existing_writer_verified` 分支仍须由安装方证明确无旧收发者；它不能用于跳过已有服务的接续门槛。

原订阅自然过期时允许已核身份、历史和外部接续证据的部署启动，以便原调用方通过现有 `events/subscribe` 校验刷新同一 ID；安装器不代刷新。启动前安全 JSON 和 LaunchAgent 注册结果明确报告 `retained_subscription_state=expired_requires_same_id_refresh`、`subscription_refresh_performed=false`，不是续期成功或 MCP 就绪。自然过期期间既有微信事件保持 queued，原 ID、正文、attempts 和时间字段不变，不投递也不阻塞其他事件；同 ID 经原挑战校验刷新后才继续。明确 unsubscribe/410 仍取消，已 failed/cancelled 的历史不会复活。

## 接入后还须由 dot 完成

已提供[唯一协调者和额度provider宿主接线接口](COORDINATOR-WIRING.md)，可在dry-run下验证真实配置选择与独立持久日志；正式宿主能力仍需核验，不能仅改配置宣称接通。

- 核新服务实际加载版本、真实 MCP 状态，再绑定仅本人可用的插件和准确的一个微信事件处理者；这些平台写入须在批准范围内。安装器不修改平台旧订阅或 AIHot 周期；这不证明本地消费者升级后继续运行。
- 普通短问直接 final；长任务 ack 后再 final。固定 ACK 关闭，旧 dot 镜像不恢复。旧消息、unknown 和已发送记录不能因新库为空而重放；本版本同一 bot 接续只支持经批准的 `retained_state`，必须保留并核验原幂等历史，不能仅靠本地 worker.lock。旧方案中的新入站切点路径尚未实现，不可用于本版本启动。
- `/help` 和别名使用已确认菜单；额度由该事件执行环境真实可用、账户已核的官方只读工具提供，四行格式不变。桥中提供 provider 适配器，但没有内置已认证的官方读取连接；本地安装成功不会把另一任务的 get_usage_limits 自动移交给当前协调者。缺工具必须返回“未调用”，不得读认证补接口、伪造百分比或用旧结果。
- 原生图片、PDF/Word 原文件、视频、分页、引用归属、Drive 先归档和逐附件回执，按现有交付契约逐项验收。原生图片预览不能被链接代替；既有 final 不吞掉用户明确要求的附件。平台拒绝终止对应动作。
- 正式切流须核验获准保留的原状态、唯一协调者和原 send_id 查重；先停旧写入者并验收再启新，禁止双写。本版本不以新消息切点代替历史保留。失败时停新，不自动恢复旧；确认新队列和 unknown 状态后，依据批准恢复原处理者，绝不覆盖数据库回滚。

## 已有 AIHot 集成的接续

公开版不携带私人部署配置，普通 CLI 默认关闭 AIHot 集成。已有启用实例不能直接用默认配置覆盖升级：必须先核实原消费者、数据目录、原批准与 cutoff、运行代际及路由，使用受支持的可信宿主注入 AihotOperatorConfig 并验证接续。该配置不能由微信正文或 MCP 参数创建，不能用示例值冒充批准；当前没有通用 CLI 配置加载器。缺接线时不得声称无损升级。原持久恢复标记存在但可信配置缺失时，通知保护会保持停止；不得清除标记来绕过，普通 owner reply 仍走原路径。

## 当前边界

安装器的离线成功证明依赖、代码、schema 与隔离启动可运行，不证明 Tunnel/插件/事件权限已授予。扫码自动继续流程和 macOS plist 注册逻辑均需真实平台及用户配合验收；官方额度 provider 接入、生产切流和手机端到端在完成前分别保持未通过。
