# mirror-journal-v1：逐消息、逐部分镜像

本地扩展构建标记为 `eventpump1.reconcile1.mirrorjournal1.localcutover1`，不修改自动任务或补发历史。模块只存 ID 和关联元数据，不读写 dot，不保存正文或凭据。已有 v1 回执和 reconcile1 对账能力保留。

## 真实切换门槛

本地事务可以给单键发一次协调许可，不能撤销旧 dot 任务已经持有的 `user_message.send_message` 能力。停 Mac、重启、租约到期、空 ledger、新聊天锚点或“已排空”布尔值都不证明旧写入者已停止。

复用 epoch1 的默认拒绝门槛：可信适配器须核实精确 epoch/nonce/目标 scope、全部旧入口的持久栅栏、完整来源掌握和在途为零。随后在同一 SQLite 写事务内捕获全部旧 owner ID，包括待附件和未有事件的消息，并激活首次入库触发器。不用时间或 rowid 判定新来源。

**verified-fence模式当前没有真实平台适配器，默认 verifier=None，因此该模式仍拒绝新写入。** 测试模拟器只验证本地算法，不能拿来激活生产。新消息只有在真实门槛成立之后，首次本地入库且不属于任何旧集合、目标 scope 匹配时，才可逐键独立处理；旧消息、迟到事件和 unknown 不因此获得重发权。

staging/activation 只保留为可信本地构造接口，不暴露 MCP 写工具。没有让模型自行提交“已排空证明”的入口。

## 唯一键和状态

source 固定为已核验的 `owner_weixin_dm`。唯一约束包含 source、绑定 account/owner、message_id、part、attachment_id；空附件用非 NULL 空串。part 为 original/ack/final，附件只能属于同一来源的 original，ack/final 须已有回复承诺。每个键固定一个目标 scope，不允许换房间重新领取。

每个附件独立建档，文本成功不代表附件成功。本契约每次 dot 写入只携带一个键的 metadata，同一 scope/目标消息 ID 不能误关联多个键。

| 状态 | 含义 | 许可 |
|---|---|---|
| prepared | 建档，未发许可 | 必须另行 authorize |
| authorized | 一次本地许可已持久发出，可能已调用 dot | 重复 authorize 不再授予 |
| unknown | 超时、丢回包、过期或 daemon 恢复后的未决 | 永不自动重新领取或重发 |
| committed | 记录实际目标 ID 和关联证据 | 同目标幂等，冲突拒绝，不再发送 |

prepare 首次和重复都不授发送权。其回包丢失可恢复同一操作身份，随后仍需一次性 authorize。authorize 回包丢失必须按可能已发送处理，不能重复领取。lease 默认120秒、范围1–300秒，不是到期可换执行者的工作租约。写方法观察到过期会持久转 unknown，即使其后的回执校验被拒绝；只读查询仅报告 effective_state，不修改状态或续租。

daemon 恢复在启动收取/发送线程前将 prepared/authorized 保守转 unknown，并挂起 staged/active 来源准入；只读构造和状态查询不恢复其他进程的状态。旧执行者的迟到回执只能对同一 attempt 对账，不能获得新发送权。

新 prepare 拒绝覆盖已有 v1 键；v1 prepare 在已有新 journal 键或已建立来源阶段后，不能新建旧 claim。双向检查与 INSERT 都在 BEGIN IMMEDIATE 内。已有 v1 记录可读、可用实际历史回执对账，不删除、不转换为重发许可。

## 五个 journal 工具

具体部署状态须从当前连接的 `tools/list` 与真实服务版本读回，不能仅凭这份文档推断：

1. `prepare_weixin_dot_journal(source,message_id,event_id,part,target_scope,attachment_id?,lease_seconds?)`：返回 journal_id、原 attempt_id、应携带的 message_metadata，claim 恒 false。
2. `authorize_weixin_dot_journal(journal_id,attempt_id,event_id)`：真实准入、owner/event/part、未过期都满足时，只有一次响应返回 send_authorized=true；它是本地协调许可，不替代用户/平台通信批准，也不是远端 API 强制栅栏。
3. `commit_weixin_dot_journal(journal_id,attempt_id,event_id,target_dot_message_id,receipt_source,evidence)`：记录实际 api_response 或正向 history_readback。
4. `mark_weixin_dot_journal_unknown(journal_id,attempt_id)`：持久停住未决，不降级 committed。
5. `get_weixin_dot_journal_status(message_id)`：精确只读，不领取、不续租、不发送；同时保留独立的旧回执投影。

## 父 dot 调用与真实验收

旧 v1 工具 `get_weixin_dot_mirror_status`、`prepare_weixin_dot_mirror`、`commit_weixin_dot_mirror`、`mark_weixin_dot_mirror_unknown` 在兼容部署中可能仍可见，但必须继续遵守原历史/unknown 规则；空记录不能跳过 partial。用 `read_weixin_message` 核验来源，正常微信 reply 工具继续与镜像解耦。新来源的 journal 准入以实际 cutover 状态为准，不因旧 v1 工具仍在目录中而回退旧写路径。

只有在本人已批准本次通信、实际准入门槛已核对且原插件目录确实包含 journal 工具时，父 dot 才能按以下步骤验收一个新来源、一个 part：

1. 读取本人原消息、事件与实际附件，核对当前目标会话 scope。
2. prepare，保存返回身份及该 part 的单键 journal `message_metadata`。
3. authorize。仅实际收到首次 `send_authorized=true`、未过 lease 且本轮事件允许发送时，用正式 `user_message.send_message` 写一次；隐藏 journal metadata 原样携带，正文不放技术 ID，不伪造 user role。其他独立 metadata 可随实际消息展示，但不属于 journal 回执证据。
4. 有实际目标消息 ID 时，可 commit `api_response`。超时、丢回包或中断则用原 journal/attempt 调用 `mark_weixin_dot_journal_unknown`，不换 ID 重发；若结果本身不明，先查原 journal 状态。
5. 用正式 `user_message.read_messages` 精确正向读回目标：同 room、未删除、`partial=false`、隐藏 journal metadata 完整吻合。以相同目标 commit `history_readback`；即使 API 回执先失败或租约后进入 unknown，也只凭这个真实正向读回对账，不重新发送。查不到不授重发权。
6. 查 journal，核实相同 attempt、目标、状态和回读证据。重复事件应返回原 committed/unknown，不能再次写入。分别验收附件及各回复阶段。

桥接自身没有父 dot 的 `user_message` 工具；必须由具备该工具的调用方完成真实正向读回。桥接保存的是调用方提交的关联证据，不能独立认证目标消息。

### 回执证据的准确包装

`commit_weixin_dot_journal` 的 `evidence` 与目标 dot 消息的**完整** metadata 是两个不同的对象。先从 `get_weixin_dot_journal_status(message_id).records` 选出同一个 part，取其 `message_metadata`；该对象的顶层必须**只有** `weixin_dot_journal`。逐字段核对真实 `user_message.read_messages` 中的同名 journal 部分后，才将这个单键投影放入 `evidence.message_metadata`。真实消息若另含 `weixin_mirror` 等独立 metadata，不要将那些额外键放进 journal 回执。不要自行补造字段、把整条 prepare/status 回包嵌进去，或用内容相似代替目标 ID 与隐藏 metadata 核对。

以下仅展示字段包装，所有值必须来自当前真实回包与读回；这段示意**不是可提交的回执**：

```text
journal_record = get_weixin_dot_journal_status(message_id).records.find(r => r.part == part)
journal_metadata = journal_record.message_metadata
# journal_metadata == {"weixin_dot_journal": {version, journal_id, attempt_id,
#   source, message_id, part, attachment_id, direction, suppress_reflection}}

api_response evidence = {
  method: "user_message.send_message", target_scope: actual_scope,
  target_dot_message_id: actual_target_id, message_metadata: journal_metadata
}
history_readback evidence = {
  method: "user_message.read_messages", target_scope: actual_scope,
  target_dot_message_id: actual_target_id, message_metadata: journal_metadata,
  partial: false
}
```

外层键必须严格相符：`api_response` 恰好四键；`history_readback` 恰好五键。后者的 `partial` 必须是 JSON 布尔 `false`，并来自支持的精确正向读回。scope、目标 ID 与 metadata 必须匹配同一 part；JSON `true` 与数字 `1` 不相等。额外正文、URL、`weixin_mirror` 或凭据字段会被拒绝。若结果未知，先查原 journal/目标，不得因回执格式错误而再发送。

桥接不能独立访问 dot，因此 receipt_basis=caller_report、target_id_verified_by_bridge=false、dot_visible_verified_by_bridge=false。positive_readback_recorded 只是正向调用方证据已记入，不是桥接独立认证。只有真实正向目标回执能解决 unknown；不能凭内容相似、完整/不完整查询未找到，或者 lease 到期重发。

## 部署与回滚约束

每次上线均须依据当次具体授权完成：原队列空闲短停、一致备份、同一实例替换/重启、既有插件刷新、父 dot 真实回执验收，之后才迁移原自动任务。不改凭据、租期、安全设置，不建第二消费者。真实旧写入者栅栏缺失时，不得把本地准入称作远端已隔离；如按用户明确接受剩余风险的有界试验执行，须限单条并在试验后关闭准入。

未启用时保留候选即可；一旦新 journal/来源阶段已使用，不能直接回退到 journal-unaware 程序继续镜像，它可能绕过新表。必须先以受支持方式持续禁止全部旧/新 dot 写入，保留所有 unknown/committed 数据；继续用理解 journal 的版本只读对账，或先准备保留 guard 的兼容回退。不能恢复旧 DB 覆盖新消息，不能以租约到期冒充排空。

没有跨服务事务或目标 API 幂等契约，本候选不承诺严格恰好一次、手机送达、完整历史已迁移或3秒响应。


## localcutover1：明确接受剩余风险的本地边界

这是与verified-fence分开的模式，不代表旧远端dot任务已经终止。用户批准有界本地切换后，部署动作hold用一个BEGIN IMMEDIATE捕获全部已有本人inbox ID为legacy，并安装AFTER INSERT触发器；之后首次入库的新ID记为reserved_new。重复/旧ID保留原分类；待附件/未有事件的旧消息同样隔离。不用时间或rowid证明新旧。

hold之后所有v1新prepare被持久拒绝，已有旧回执仍可对账。部署只保持closed，不自动发消息、不补历史。closed期间照常收微信、发送正常回复，新源已归journal所有但不能取得新dot发送许可。进程重启保留边界；journal在途仍保守unknown，不续租或重领。

父dot先调用get_weixin_dot_cutover_status({})读取真实boundary_id，再确认新工具已可调用且实际target_scope对应当前会话。父代理将原任务切到新流程后，才调用enable_weixin_dot_cutover_new_sources({boundary_id,target_scope})。它仅开启reserved_new的本地准入，scope不可更换、旧键永不变新。没有删除订阅或创建新账号。只有新源且part仍prepared、未过lease时，后续一次authorize可许可。

风险始终明示：mode=bounded_local_ownership、remote_legacy_writers_fenced=false、remote_legacy_direct_write_risk=true、strict_exactly_once=false。本地guard不能阻止旧远端任务绕过MCP直接调用dot发送API；接受该风险不等于真实排空证明。父端仍必须停止旧镜像入口并只使用新流程，未知发送先正向读回，不得盲重发。

回滚不能换回忽略cutover/journal表的旧程序后继续镜像。若升级失败，先保留当前新guard和closed准入恢复收发，保存全部状态；不恢复旧DB或解除持久旧入口锁。不应为了恢复镜像而静默打开旧claim。
