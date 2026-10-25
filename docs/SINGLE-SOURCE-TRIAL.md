# singletrial1：一条未来来源的候选验收契约

本目录是候选源码；它本身不证明任何运行实例的当前准入、主 Dot 读回或手机送达状态。真实部署、登记试验、选定来源和外发都要按当前授权边界及最新只读回执确认。本说明优先于复制的历史部署说明中的批量 enable 与过时回退配方。

## 两步选择与持久隔离

第一步 `arm_weixin_dot_single_source_trial` 要求当前 closed、当前 expected_revision、真实同一 Dot 的 target_scope、稳定 request_id，以及一条唯一合成短文的 UTF-8 SHA-256。登记推进修订号并固定 scope。入站触发器只记录登记事务之后新 INSERT 的本人来源，既有 legacy/reserved_new 不进入试验。重入站旧 ID 不算未来来源；这里的“未来”指本地首次入库，不以不可靠的上游时间戳判断。因此须登记成功后再由本人发送唯一标记。

`arm` 只能登记一次，`target_scope` 永久绑定；**不能用临时测试房间**消耗这次试验，再期望切换到正式 Dot 目标。登记前由本人核定将来实际使用的同一 scope。close、回退或重启都不会释放这次选择；一次通过不自动打开持续镜像。

若现有 journal 已有 authorized 意图或旧 epoch 仍 staged/active，arm 会拒绝；须保留原许可与 unknown 正向对账，不清表或换调用路径绕过。

第二步 `select_weixin_dot_single_source_trial` 在主 Dot 读到那条消息后，使用精确 message_id、原 request_id 和 arm 后的当前修订号。选择必须同时满足：属于登记后的本人新来源、正文 SHA-256 匹配、1–256 UTF-8 字节、入站清单 complete、没有附件、没有历史 ledger，且不存在 unknown/已授权/已提交/授权历史不明的 journal。允许此前只准备过但可证明从未授权的记录，不能凭空重领历史许可。

选择与修订号推进同一个写事务；一次试验只能选一个 message_id。它允许这条来源的合法 original 和实际回复槽位，不能允许其他来源。**批量 cutover.state 始终 closed，new_admission_enabled 始终 false。** 看是否仍有单条许可可能，应读 single_source_admission_enabled / any_new_admission_enabled 与 single_source_trial.selection_active。不要只看 closed 就声称所有新许可均关闭。

旧版本代码只认识 closed，会拒绝授权；数据库持久 trigger 还会拒绝 trial 存在后的任何 new_only 更新。journal 的持久 SQL guard 核对选中来源、固定 scope、closed 和 selection_revision。无 trial 的原有批量契约保持兼容；一旦登记 trial，批量 enable 永久拒绝，旧包也不能绕过。

arm/select 的响应丢失只能重读同一决定；不能移动未来边界、换消息或重开。正式 close 的稳定 request_id 推进修订号，立即使单条选择失效；其后重复 arm/select 只返回已记录事实。关闭不能撤销已发出的 permit。重启保留该状态，不会自动关闭仍活动的单条试验。结束试验必须明确 close 并读回 any_new_admission_enabled=false，再逐个按原 journal/attempt 对账未决许可；unknown 继续保留，不能宣称后续远端写入已不可能。

## 一次性限制与兼容回退

这是一次性验收限制：没有删除、重置、换来源或重新登记第二轮试验的工具。trial 及其 future membership、SQL guard 必须保留。本源码另含[单条验收后未来来源准入候选](STEADY-FUTURE-ADMISSION.md)，须单独审查迁移与授权；不能从一次 trial 自动开启常规准入，禁止删除表或改数据库绕过。登记前的代码切换可回退，已登记的限制不能通过回退撤销。

正式支持的兼容包要保留新版 weixin、dotjournal、dotcutover 三个模块，其中 dotcutover 必须是本次 singletrial1 版本。它只能撤回完整性聚合等外围整合，不能回退三个保留模块本身的故障。不恢复旧数据库，不删除新表/列/触发器，不清空 unknown/许可/回执。冻结的旧 guardedrollback1 仅用于证明旧代码对新限制 fail closed，不能据此替代最新完整回退发行包或用其 status 证明 trial 已关闭。

## 主 Dot 的逐消息验收

主端先核对真实新工具目录、入站消息身份、唯一合成标记、当前 trial 修订和 scope。每一部分使用既有稳定 journal key：prepare → 一次 authorize → 一次对应 Dot 写入 → 原目标 ID commit → 同目标的可核验读回。新来源首次许可不依赖整个不相关聊天窗口完整，也不能把空搜索或 partial 当作重发依据。某部分一旦许可已发或状态 unknown，只能保留并以正向关联目标回读对账，不能再 authorize/换 attempt/重发。

每个 Dot 写入结果和手机微信 final 分别验收。原文与实际 final 都要核对 expected parts；committed、readback_reported 不等于正文已核验。平台不能读到目标内容时，该部分仍未验收。简单短文只回一条 final，不人为添加 ACK；不要为附件覆盖而扩展第一次真实试验，一条短文不能声称验证全部附件链路。

本地试验限制不阻止旧远端任务直接调用 Dot API。若要求严格唯一 writer，真实写入前须有可验证的旧镜像执行者终止/目标侧写入隔离及在途排空证据；配置提示词和本地 guard 均不能代替远端技术栅栏。没有这些证据时，不宣称严格恰好一次，也不翻转 `remote_legacy_writers_fenced`。

若平台无法给出全局排空证明，可**另行明确批准一次有剩余风险的 bounded-local 合成试验**：先只把原入站任务的旧直接 Dot 镜像分支约束为只读并读回生效配置，保持 webhook 订阅和正常微信回复；再由本人批准正式固定 target scope、唯一未来短文、**新 journal 路径**最多原文/实际 final 各一次及现有回复者的一条本人微信 final。本人须明确接受旧远端任务仍可能额外或重复直写、**次数未知且不受新路径上限约束**的风险。新来源仍需本协议的精确清单、journal 单次许可和同目标 `partial=false` 内容读回；旧历史 partial 保持 unresolved，旧游标不推进。提示词更新不能取消已在途旧回合，所以此路径只能报告该次有界观测，不能报告旧 writer 已隔离、无重复硬保证或持续同步上线。未取得上述针对性批准时保持准入关闭，不试写。
