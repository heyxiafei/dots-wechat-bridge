# 运行与恢复

## 读取、回复与完整性

优先使用已运行服务的只读 MCP 工具，核对实际版本、本人绑定、现有订阅、回复状态和 cutover 状态。不要把候选 `probe.py --status` 指向生产当只读预检：构造 Probe 可能初始化或迁移 schema。不要读取私有完整日志或凭据来补齐状态。

用 `read_weixin_message` 核对精确来源和 `reply_state`。简单请求使用 `reply_weixin_message` 承诺一条 final；复杂任务才用 `reply_weixin_message_phase` 的 ack → final，重入时保留已承诺文本和稳定 ID。实际调用前检查当前工具 schema，不猜参数或把 `next_phase=ack` 当简单消息必须发 ack。

`get_weixin_dot_completeness` 从源事实列出原文、附件和实际承诺的回复。核对源内容可用性与目标状态：缺失附件、unsupported、超限、截断或 legacy_unrecorded 都要明确保留。附件最多 8 个、文本最多 65,536 UTF-8 字节；更多项采用有界处置记录，不能提高限额或将有界前缀称全文。

`all_parts_accounted_for` 可以包括已解释的失败；`receipt_recorded`、`readback_reported`、committed 也不等于目标内容已验证。对附件需实际读取字节并校验；对 Dot 需精确目标 ID、同 scope 的完整正文及 metadata 读回。历史窗口 `partial=true` 不能冒充完成。

## Unknown 与关闭

曾授权、授权回包丢失、发送后未 commit、显式 unknown 或授权历史不明的 journal，不因租约到期、无 target ID、空搜索或重启重新授予许可。只允许对有正向关联证据的原目标对账；不能追发旧来源。真正从未授权的 prepared 恢复必须使用协议的原子换代能力，旧 attempt 随之失效。

新关闭意图使用稳定 request ID 调用正式 close，读回当前状态和修订号。同请求的重试返回历史决定，不一定证明该决定仍有效；检查 `close_effect_is_current`。关闭推进修订号，拒绝旧 enable，但不能撤销已发 permit 或远端直接写能力。

singletrial1 下批量 `state=closed` 仍可能有活动单条选择；核对 `any_new_admission_enabled=false` 和 `single_source_trial.selection_active=false` 才能报告全部新准入关闭。重启不会自动结束活动试验。

## 生产切换与回退

先交付具体差异、源文件哈希、离线测试、兼容回退包和影响，再按既有授权执行。所需但尚缺的授权应精确限定到目标服务短停、备份/增量迁移、文件替换和同实例重启；真实发送另按其测试范围确认。

必须知道服务、client/worker、当前 PID、锁与托管关系。历史 PID、`launchctl list` 非零退出或看到进程打开 lock 文件，都不足以证明当前服务身份与独占锁。身份不明就报告需要现场确认的内容，不盲停、杀进程或绕过系统拒绝。保持持续订阅；不要另启第二个消费者，也不要与其他任务同时改写共享桥接。

获准后先确认准入关闭与在途/unknown 状态，停止准确实例并核验退出，保留私有一致快照作为取证和灾难恢复材料，再安装冻结文件、增量迁移并启动同一实例。核对绑定、订阅、回执及新增消息保留。工具发现需分别验证服务端 schema、目标主 Dot 目录和目标主 Dot 的真实只读调用；子任务工具成功不能代验。

singletrial1 的兼容回退必须保留新版 `weixin.py`、`dotjournal.py`、singletrial1 `dotcutover.py` 及其依赖，连同当前数据库中的 guard、trial、授权事实、新收件与回执。先正式 close 并核验全部新准入关闭，再换已实测的兼容代码。禁止整套旧代码回退、旧数据库覆盖、删除新表/触发器或清空 unknown。保留模块本身有缺陷时应保持关闭并修复前进，不能用不带保护的旧模块替换。

可回退的是经过验证的代码组合；远端已发送内容、已发许可和 trial 的一次性限制不会随回退消失。报告阻塞时写清动作、目标、原因、已完成项，以及 denied、pending 或 uncertain；结果未知先查回执，不盲重试。
