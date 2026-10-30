# 唯一协调者与官方额度接线

本文件描述已实现的宿主接口。它不等于当前云端任务已加载这些代码，也不使本机Python自动获得另一会话的工具。

## 必须由可信宿主注入的能力

`probe.owner_coordinator.from_host_configuration` 把配置接到实际宿主对象：

- `caller_id` 是当前执行任务的真实身份，须等于配置的唯一 `coordinator_id`；不能从微信正文提取。
- `call_tool(name,args)` 是宿主原生、受审批检查的微信读取/发送适配器，返回解包后的真实工具结果；它仍是同步接口。固定命令的 async 入口仅 await 已注入的官方额度 reader，不会把 async 微信工具自动变成同步工具。异步微信宿主仍须提供其受支持的桥接层，不自行暴露HTTP代理、密钥或新持久会话。平台明确拒绝由适配器抛 `PlatformDenied`；其他异常保持unknown，不能改ID重发。
- 可选宿主参数 `reply_projection_supported=True` 仅在实际工具 schema 支持 `projection='reply'`，且适配器会读取并返回完整 `structuredContent` 时启用。默认False保留旧入参。启用后省去重复正文和独立镜像inventory，仍保留owner、引用、附件、捕获完整性及原reply_state；缺安全字段立即停止，不自动改用full重复调用。不能从消息、配置JSON或工具摘要自证该能力。
- `permit(scope,tool)` 每次写入前核验当前处理者/切流代际、旧写入者已隔离、本人请求和附件授权。必须来自可信执行上下文，不得默认true。无此能力即停止发送。本地flock仅防同目录多进程，不证明远端或旧服务已停止。
  返回 False 或缺失能力只表示等待。若可信宿主明确抛出 `PlatformDenied`，协调者在调用发送前持久保存原动作的拒绝；后续调用不清除、不重发。其他异常不等于平台批准或明确拒绝，不建立新的提交。
- `binding` 是已核本人→当前官方账户的 `UsageBinding`，配独立证据引用；不能用用户发来的JSON或子任务转述自证。
- `official_tools` 是当前宿主实际可调用的工具注册表。默认只选 `mcp__codex_app__get_usage_limits`，缺失不调用；不借另一任务的工具宣称已接通。
- 如确有此前获准的App Server会话，可显式选择 `account/rateLimits/read` 并注入该会话已有RPC调用器；不打开进程/会话，不读取认证，不登录、不刷新token、不消费reset额度。不是HTTP地址，且不会在拒绝后切换provider。
- 可选 `credit_mapper` 仅从实际结果中按已核字段映射构造 `CreditInfo`。缺失时第四行余额未知；不可把reset次数当余额或把空值当0。主机工具可用同步或 `prepare_usage_async` 路径，async调用被实际await且只调用一次。

配置例子在 `config/owner-coordinator.json.example`，默认dry_run、无任务ID、固定ACK关闭、旧dot镜像关闭。JSON不能授予权限或凭据。

## 已接入的行为

固定命令由可信宿主调用：

```python
# scope 来自宿主已核实的本人请求与账户映射，不来自消息/hint/用户JSON。
result = coordinator.handle_fixed_command(
    message_id, scope=trusted_scope, binding=verified_binding,
    owner_intent_verified=True,
)
# 若官方额度reader为async，调用相同参数的入口并await：
# result = await coordinator.handle_fixed_command_async(...)
```

`trusted_scope` 必须有 literal `verified_scope=True`、匹配 `request_ref=message_id` 和 `evidence_origin=direct_owner_message` 或 `trusted_parent_transcript`；额外的 `owner_intent_verified` 必须是 literal True。额度读取还要求 `scope.owner_ref/account_ref` 与 `UsageBinding` 相符、provider 有可信绑定证据。`read_weixin_message` 不暴露账户映射，`accountId:null` 也不能补出映射；这些核实义务属于实际宿主。

入口重读原 owner 消息并重新分类，拒绝引用、混合附件、不完整捕获和其他命令，不用 `fixed_command` hint 自授权。完整的独立“额度／查额度／查一下额度”与既有周额度别名只指向 Codex 周额度，不追问类型；`/help` 复用现有菜单。缺 reader/账户证明仍生成明确“未知、未调用”的四行结果，经原 final/permit 路径处理。实际官方读取拒绝则停止并返回四行错误文本给宿主，不由 handler 发送；拒绝记录即使在 dry_run 下也持久保存，因为 dry_run 仍可能实际执行获准的只读额度调用。dry_run 不发送微信，不生成发送 claim；它不是免授权的离线模式。

读取前同时核查远端 ack/final 和本地 ack/final 的 claimed/unknown/denied/submitted；已有记录优先协调原 ID，不读新额度造成内容冲突。sync/async 同消息共用 single-flight 标记，不持线程锁跨 await；取消只释放读阶段标记，不清除已存在的持久提交。读取后重新核验消息、scope、原记录，再经原 permit 提交；并发普通回复已占槽则查询原提交，不另发额度。同步入口遇 async reader 返回 `requires_async_usage_reader`，不提交虚假结果。

`reply()` 先调用现有 `read_weixin_message` 核owner来源及当前ack/final，再分派原幂等回复工具；短问直接final，长任务ack→final，已有final查原send_id。没有notification回退。

新回复须可编码为 UTF-8、非空白且不超过8192 bytes，与现有微信工具限制一致。无效新文本在claim/permit/发送前失败，宿主可修正尚未提交的内容后再准备；不自动截断或分片。若原槽或本地账本已承诺、拒绝或未知，先保留原动作/ID；坏的新文本不能掩盖原状态，修正文案也不能解除它。

`media()` 独立核原请求、逐附件授权/实际SHA/格式及Drive归档，核已完成的上传元数据，再用稳定附件键调用原 `send_weixin_media`。PNG用image，PDF/Word原文件用file，视频用video；BLEND默认链接，明确请求原文件才走file。已有文字final不阻止附件。上传及Drive归档仍由原授权工具完成，适配器不绕过上传/平台审批。

附件 `kind` 必须与可信scope一致。`model_source` 转原文件要求item和scope的 `original_requested` 均为 literal True；manifest单方改类别或把False/缺失改True不能扩大授权。缺少这些范围字段时宿主应从原可信请求补齐，不能按待发内容倒推出授权。设计来源与未采纳部分见[设计对照](POCKET-DESIGN-REVIEW.md)。

每次提交前先持久记录claim。工具超时/崩溃/丢失返回保持unknown，下次查原ID或协调原提交；不再次调用发送。真实拒绝持久保存，不能靠更换provider/工具恢复。附件及文字独立键，accepted与本人收件证据分开。此版本不自动清除denial；任何新批准重试仍交原平台审查和明确人工协调，不制造第二发送路径。

重新生成的文字或附件摘要与原记录不符时，返回原denied/unknown/claimed/submitted的处理动作与原ID，同时标记 `content_conflict=true`。该标志表示新内容没有提交；查询到原accepted不能作为新内容已发出的证据。不能让内容差异掩盖原拒绝或未知提交，也不覆盖原digest、删除记录或换通知工具。

接线不能只通过“把配置mode改execute”完成。正式环境需要实际可用的宿主适配器与permission/fencing实现、唯一任务身份和本地持久日志位置；都核实后才允许执行模式。

## 当前项目绑定建议

复用现有微信协调任务，不另建第二个写入任务。父任务持有实际当前任务信息，并在更新前读回prompt，以精确补丁保留其他规则、enabled和完整triggers。平台插件或事件执行环境若没有加载本机模块/提供原生调用适配器的受支持入口，则保持未接通，不能另开未经批准的服务来转发这些工具。

官方依据：

- [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)：管理与运行权限分开，运行需要Tunnels Read+Use；权限是Platform组织范围；客户端主动出站，无需公网入站。
- [Codex App Server](https://learn.chatgpt.com/docs/app-server)：`account/rateLimits/read`是读取账户额度的RPC；窗口时长/usedPercent/reset按真实字段解释，不能固定把primary当五小时。
