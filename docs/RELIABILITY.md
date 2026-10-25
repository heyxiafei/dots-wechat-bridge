# reliability1 候选契约

本副本增加 singletrial1，一条未来短文的验收以 [单来源契约](SINGLE-SOURCE-TRIAL.md) 为准。以下批量 enable 仅适用于从未登记 trial 的数据库；trial 存在时该入口永久拒绝。关闭状态还需核对 any_new_admission_enabled=false，避免误把 bulk closed 当作单条试验已关闭。

本版本只在隔离候选中开发与离线验收；没有部署、开启生产准入或真实外发。本页是 reliability1 的当前契约，替代旧文档中“prepared 过期一律 unknown”和无修订号 enable 的描述。手机送达、主 Dot 工具目录、远端严格恰好一次仍须另行验收。

## 原子关闭与防迟到开启

`get_weixin_dot_cutover_status({})` 返回 `admission_revision`。开启调用为 `enable_weixin_dot_cutover_new_sources({boundary_id,target_scope,expected_revision})`；`expected_revision` 必须来自当前状态。关闭调用为 `close_weixin_dot_cutover_new_sources({boundary_id,request_id})`，请求 ID 对同一个关闭意图保持稳定。两者与 journal authorize 都使用 SQLite 写事务。

关闭推进修订号；关闭前的 enable 参数不能重新开启。相同 close 请求重试只返回其已应用事实和当前状态，即使后来已明确重新开启，也不撤销后来的决定。新的关闭意图必须用新请求 ID。关闭保留 scope、边界、来源分类、旧 prepare guard 和全部回执；不能撤销已经授予的本地许可，也不能封锁直接调用远端 Dot API 的旧任务。已绑定 scope 不允许通过关闭再开启更换。

持久数据库触发器拒绝旧代码忽略修订号的开启 SQL。无新字段的旧客户端收到明确参数错误，不能静默按旧语义开启。工具 schema 有变化，正式升级时须核对目标主 Dot 的真实工具目录，子端不能代验。

此版 enable 仍覆盖整个既有 reserved_new 集合及未来新来源，不是单键开关。真实小样本前必须另外限制执行范围，不能顺带处理积压的新来源。

## 可证明从未授权的准备态恢复

新协议为新建 journal 保存授权历史标记和永久授权事实。只有新协议下创建、从未发出许可、仍为 prepared 且已到期或启动恢复标为失效的记录，才允许通过同键 `prepare_weixin_dot_journal` 取得新的 attempt。更新 attempt 和失效标记在一个事务内完成；旧 authorize、commit、mark_unknown 的 attempt 被拒绝。尚未失效的重复 prepare 返回原准备身份。

已授权、显式 unknown、committed 和旧协议历史不明记录都不能以空 authorization_id 或租约到期换取新许可。旧 prepared 不自动推定从未授权，保守显示授权历史未知；历史 unknown 不迁移为可恢复。已授许可的事实由持久单调保护保留，不能清空字段、换 attempt 或重启来重领。

授权回包丢失、发出后 commit 前退出，继续按 unknown 处理。同目标 commit 可幂等恢复，冲突目标拒绝；positive history readback 仍只是调用方报告。启动恢复在 worker 启动前执行，不因重启发新许可。

## 从源事实派生 expected parts

新只读工具 `get_weixin_dot_completeness({message_id})`，以及 `read_weixin_message` 中的 `dot_sync_completeness`，从 inbox、入站 manifest、附件行和实际承诺的回复槽位生成清单，再关联 journal/旧 ledger。

- 原文条目总是存在，纯附件消息也有来源条目；每个附件使用稳定 attachment ID。manifest 有附件而附件行缺失，仍列为 missing。
- 简单 final 不凭空要求 ack；实际承诺的 ack/final 动态进入清单。未发送或已取代的固定系统 ACK 不作为实际答复；已发送/结果未知的系统 ACK 保留其历史事实。
- unsupported、超限、截断和历史未记录清单都有独立处置项。每项分别返回源可用性和镜像状态，附件过期不会被文字成功覆盖。
- `missing/pending/unknown/unavailable` 表示仍有缺口；`receipt_recorded` 只表示有回执；`readback_reported` 只表示调用方提交了相关 metadata 的正向读回。它们都不等于正文、字节或用户界面已核验。
- `all_parts_accounted_for` 仅表示各项已有明确处置，失败也可被解释；`all_expected_parts_receipted` 和 `all_expected_parts_readback_reported` 仅总结相应证据。`all_parts_verified`、`target_content_verified_by_bridge`、`dot_visible_verified_by_bridge` 固定为 false：桥接没有独立远端内容验证器。
- `source_inventory_complete` 描述入站清单是否完整；`source_content_ready_recorded` 描述本地数据库记录的可用性，不代替实际字节读取。历史没有 manifest 的来源明确为 legacy_unrecorded。
- 读取使用一致数据库快照，`snapshot_sha256` 是元数据快照摘要，不是远端内容哈希。新增附件/回复或状态变化会进入下一次读取，不保存永久 complete 标记。

该工具不 prepare、不创建发送许可、不续租、不重放。读取清单失败时普通微信读取保留业务能力，同时明确返回不可用和未核验。

## 入站资源边界与显式缺口

入站 text 仍限 65,536 UTF-8 字节，保留合法字符边界上的有界前缀，并记录源字节数、保存字节数和截断原因。无法准确编码的输入明确标无效，不能称全文归档。不把超长全文另传服务。

下载附件仍最多 8 个；第 9 个及以后记录数量限制，不下载、不提高限额。同名附件按原 item 序号生成不同稳定身份。未知类型保存有界类型标识和 unsupported 状态，不解释其中的 URL 或执行内容。

入站 manifest 最多 64 个逐项记录，更多 item 使用稳定范围占位与准确数量；不会伪装成全量逐项保存。manifest 不保存正文、context、密钥或媒体定位 URL；新增独立表与 inbox、媒体记录、cursor 在同一事务内提交。旧 inbox 列布局保持兼容。

## 验收与回退

离线测试使用临时合成数据库、假时钟、并发 barrier 和 fake adapter。结果以本轮日志为准；此前的 320 项只作原基线。完整回归和 schema 检查通过也不构成真实微信/Dot 送达证明。

回退必须保留新版 journal/cutover 安全模块、其依赖和所有新增状态；不能整套退回忽略新历史事实的旧代码，不能恢复旧数据库覆盖新收件。具体授权后的步骤与待验收边界见 [部署与兼容回退计划](RELIABILITY-DEPLOYMENT.md)。
