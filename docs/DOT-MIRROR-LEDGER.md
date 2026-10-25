# ledger-v1: 微信到 dot 的镜像回执

本候选构建标记以 `eventpump1.reconcile1.mirrorjournal1` 结尾，不是上游新版本。账本只记录元数据，不发送 dot 或微信消息，不新增服务、凭据或访问权限，不改变既有审批和发信规则。只覆盖已有本人微信事件到 dot 的原文、附件及回复阶段；dot 主动聊天到微信的 notification 仍使用原 outbox，不伪造微信来信根。

## 身份与最小记录

唯一键是已有本人 `message_id + attachment_id + phase`，空附件内部使用非 NULL 空串。phase 为 original/ack/final，附件只属于 original。prepare 核对绑定本人 inbox、其 event_id 与 pings 的 weixin.message 关联；附件必须属于同一消息和本人；ack/final 必须已有对应回复承诺，包括合法旧 final 的 NULL source_message_id。

保存 mirror_id、源消息/事件/附件 ID、phase、三态、普通关联 attempt_id、真实目标 dot message ID、回执来源、时间和固定反射标记。没有正文、签名、key、token、callback URL。direction 固定 weixin_to_dot，suppress_reflection 固定 true。目标 ID 只接受当前已核实形态 `Sentinel_` 加32个小写十六进制字符；这只是语法门槛，不能证明远端存在或属于正确房间。

## 四个本地元数据工具

1. `get_weixin_dot_mirror_status(message_id)`：返回合法源 event ID、版本和 receipt records。tracked 只表示存在记录；all_parts_verified 始终 false，不能推断全部阶段、附件或 dot 可见。
2. `prepare_weixin_dot_mirror(message_id,event_id,phase,attachment_id?)`：事务 INSERT OR IGNORE 竞争唯一键。只有首次创建返回 claim_granted=true 及 attempt_id；该 ID 是操作关联，不是授权凭据。重复 prepare 返回原 mirror_id、claim_granted=false、attempt_id=null。本候选的状态读取另有 `reconciliation` 区域，只提供已经存在的原 attempt 等对账元数据，恒定不授予新 claim 或对外写入权，见[对账契约](EVENT-RECONCILIATION.md)。
3. `commit_weixin_dot_mirror(mirror_id,attempt_id,event_id,target_dot_message_id,receipt_source)`：receipt_source 为 api_response/history_readback。只将真实受支持 API 响应或明确关联的合法历史读回所得 ID 记录为 committed。必须匹配原 attempt 与事件；同目标幂等，不同目标冲突，不覆盖。prepared 可记录两种来源；unknown 只接受 history_readback。
4. `mark_weixin_dot_mirror_unknown(mirror_id,attempt_id)`：prepared→unknown；重复 unknown 幂等，committed 不降级。无 reset、reclaim、retry 或自动补发接口。

## 调用方责任与崩溃窗口

先读取并核验原文/实际附件与目标 dot 房间，再 prepare。空账本或首次 claim 不证明 dot 从未展示该内容；对既有/回填事件，任何首次写入前必须用实际受支持的历史读取核验，不重发旧附件。仅在原请求明确授权、首次 claim 成功且已有展示已妥善核对时，调用方才可用真正支持的 dot 能力展示一次；如已有历史镜像，则只对实际历史目标以 history_readback 记录回执。历史工具不可用或只给空摘要，不构成“未展示”证据。技术 ID、attempt_id 和防反射标记只放真正支持的 message_metadata；若 API 不支持则保留本地元数据，不塞正文，不伪造 user role，不修改历史消息。

拿到真实 API 回执后 commit。任何超时、丢失响应、进程中断或重复 prepared 都不允许自动重新展示。唯一 daemon 重启将 prepared 标为 unknown；构造只读状态对象不会执行该恢复。unknown 的解决需要合法历史读回的真实目标 ID，并用实际 message_metadata 中的 attempt_id，或原 prepare 结果加明确原请求关联核对。只有相同正文不足以唯一认定。若历史、元数据或原关联均不可得，保持 unknown，不猜 ID、不重发。

账本不能独立访问 dot，也不能验证调用方报告的目标存在、角色、房间、内容或可见性；所有回执显式 receipt_basis=caller_report、target_id_verified_by_bridge=false、dot_visible_verified_by_bridge=false。attempt_id 防止误关联，不构成密码学证明或额外账户鉴权。

本地事务只保证唯一准备、稳定记录和冲突拒绝。dot 写入成功但本地 commit 前崩溃，或首 prepare 回执丢失，仍有未知窗口；没有共同事务或目标系统幂等契约，不能保证跨产品恰好一次。微信 API accepted、dot 本地 committed 回执和本人手机确认是三条独立证据；webhook 2xx 也不是 dot 展示成功。

新逐部分意图候选见 [DOT-MIRROR-JOURNAL.md](DOT-MIRROR-JOURNAL.md)。旧表用于保留和对账；新键不能用旧 prepare 绕过来源准入或一次性授权。默认没有真实旧写入者栅栏，不能激活新发送路径。
