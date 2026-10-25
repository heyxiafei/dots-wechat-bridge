# steadyfuture1：单条试验后的未来来源准入候选

这是**离线代码候选**。它不改变任何现有运行实例、自动化、订阅、账号、固定 ACK 或 Dot 平台权限。只有单条试验在同一正式 target scope 完成、正式 close 且旧直接镜像路径按本人批准受限后，才能另行审议生产迁移和启用。桥接只能管理本地 journal；旧远端任务可绕过它直接写 Dot，因此状态必须继续报告 `remote_legacy_writers_fenced=false`、`remote_legacy_direct_write_risk=true`、`strict_exactly_once=false`。

## 准入边界

- 保留既有 `singletrial1` 记录、legacy/reserved_new 分类、journal guard、unknown 和全部回执。steady 使用与 trial **相同的不可变正式 target scope**，不重置 trial，不开放旧 bulk `new_only`。启用前须有 trial 原文及实际 final 两个已提交、已记录正向完整读回的 journal 部分，且所选来源无未决授权/unknown；这些读回属于调用方证据，桥接仍不能独立证明远端显示或手机送达。
- 每次明确启用新一代未来来源，须提供稳定 request ID、当前 admission revision 和限定的 `max_sources`。只有**启用事务提交后首次本地 INSERT**的绑定本人消息可进入该代；旧消息、重复投递旧 ID、停止期间入库的消息不能事后补入。
- 同一时刻最多一个 active 代。暂停或正式 close 使该代失效、修订号推进；再次启用必须建立新代，间隙来源保持排除。既发许可不撤销，unknown 只按原 journal/attempt 和目标 metadata 正向读回，不重新 authorize 或补发。
- 每个来源的原文、附件和实际 ack/final 各有稳定 journal key 与独立状态。入站**库存清单**不完整时不得首次授权；清单完整也不代表媒体字节已缓存。原文文字部分可在附件仍排队时独立处理。附件部分须逐件达到 `cached`、未过期且有合法 size/SHA 元数据；调用方应在**请求该部分授权前**通过 `read_attachment_chunk` 读完实际字节并核全文件 SHA，避免领取许可后才发现字节不可用。桥接授权处校验的是缓存状态和完整性元数据，不能替调用方核验 Dot 实际上传内容。某附件 pending/error/expired 只停该部分，不补发其他已授权部分。普通微信回复与 Dot 镜像独立，镜像 `partial` 不推进旧历史游标。

## 当前候选工具顺序

1. 由原主 Dot 先读 `get_weixin_dot_cutover_status({})`，确认 trial 已选一条并正式 close、原文和实际 final 的 caller-reported 正向完整读回均记录、所选 source 无 authorized/unknown。桥接不能独立验手机送达；本人确认与持续风险授权是另外的关卡。
2. 经限定生产迁移和启用批准后，使用当前 `boundary_id`、已固定的 `target_scope`、稳定 `request_id`、刚读的 `expected_revision`、`max_sources`（1..1000）调用 `enable_weixin_dot_steady_future_sources`。同一请求重读原决定；不要换 ID 越过结果未知。批量 `state` 保持 closed。读回 `steady_generation.generation_id`、`admission_active`、`source_count`、`remaining`、`steady_admission_enabled` 与 `any_new_admission_enabled`。
3. 每条 future source 只按原消息和事件 ID、完整入站清单与实际已承诺回复阶段操作现有 journal。对每个部分最多一次许可；未知 Dot 写入保留原 journal/attempt/metadata，合法关联目标历史若缺失或 `partial` 仍为 unknown，不重发。微信 final 只按自身原 `send_id` 查询。
4. 暂停时以新稳定 `request_id` 与当前 `expected_revision` 调 `pause_weixin_dot_steady_future_sources`。正式 `close_weixin_dot_cutover_new_sources` 也同时使当前 steady 代失效。两者都不撤销已发许可；读回准入关闭后继续对账 unknown。要继续收新来源，须另行批准并以新 request ID、当前 revision 建立**新代**，此前暂停间隙的消息不自动补入。重启不重置 active 代，也不重发已授权/unknown。

上限满时不再捕获新 source，但已捕获成员的未决部分仍按原 journal 状态处理。旧版本程序不认识代际标记；持久 SQL guard 让它对 steady source 的新授权失败关闭。回退前应先正式暂停/close 并读回，同时保留当前数据库和所有新增表/触发器；不能拿旧包恢复自动镜像。

## 验收和范围

状态应区分 bulk、一次性 trial 和当前 steady 代的准入，`any_new_admission_enabled` 包含任何仍活动的本地许可。`active` 或 `closed` 都不是远端 Dot 已可见或手机已收的证明。每部分仍须原目标 ID、同 scope 正文/metadata、`partial=false` 完整读回；微信 final 的 API accepted 还需本人手机确认。

离线测试使用合成 SQLite、fake Dot/微信和不同连接验证代际、限额、并发暂停与许可、重启、重复事件、unknown、旧来源和间隙排除、附件逐件及正常回复独立。即使离线通过，生产迁移与持续准入均需针对原实例、固定 scope、最大来源数、远端剩余重复风险和回退边界另获批准。没有目标侧写入隔离或原子幂等 API 时，本候选**不提供严格恰好一次**；不要靠改标志或新增凭据冒充隔离。
