# 一条未来本人消息的 singletrial1 验收

这是一条纯文本来源的候选验收协议，不是开放常规镜像。只对当前用户明确授权的测试操作执行；已获明确批准的相同范围不重复请示。普通微信收发、离线测试或手动通知成功，都不能作为此候选的真实闭环证据。

## 写入前的条件

- 确认生产使用冻结的 singletrial1 代码，且当前目标主 Dot 已看见匹配的新工具 schema，并成功调用只读入口；不能以工具数量或子任务可调用代替。
- 从目标产品实际上下文核实同一 Dot 的 target scope，不能从聊天摘要或新造 ID 猜测。
- 明确选择验收口径：严格唯一 writer 需可验证的旧执行者终止/目标侧隔离与在途排空；本地 SQL guard 不限制旧任务直接调用 Dot API。若平台无法取得全局证明，只能在本人明确批准**一条有剩余风险的 bounded-local 合成试验**、接受旧直写可能额外或重复写入且次数未知后，将原入站任务的旧直接镜像分支改只读并读回新配置，保持订阅和微信独立回复。提示词不取消在途旧回合；仍报告远端未隔离和非严格恰好一次。
- 先用已存在的无敏感目标验证精确 ID 读回能力，正文与相关 metadata 必须完整，`partial=false`；全局搜索为空或窗口缩小仍 partial 不满足条件。
- 明确本次最小范围：一条本人新短文、现有回复者最多一个微信 final，以及**新 journal 路径**分别对应原文和实际 final 的最多两次 Dot 写入；旧直写不受此上限约束。失败/未知时停止后续镜像许可，不补发已授权部分。

若条件未齐，继续离线准备并报告缺项；不要盲停服务或写 Dot。

## Arm、本人发送、Select

先只读当前 boundary、scope、revision 与全部准入状态。`arm_weixin_dot_single_source_trial` 使用当前 `expected_revision`、真实 scope、稳定 `request_id` 和唯一无敏感短文的 UTF-8 SHA-256。短文长度 1–256 UTF-8 字节，无附件。

该 `scope` 一旦 arm 就永久固定，不能拿临时房间做预演，再换成正式目标；close/回退也不能重置。需要本人先批准真实长期目标及这一不可逆限制。

若已有 authorized journal 或 staged/active epoch 导致 arm 拒绝，保留原许可与 unknown 正向对账；不清表、换 scope 或换路径绕过。

必须在 arm 成功确认后才由本人发送该短文。“未来”指 arm 事务后首次本地 INSERT，不是上游时间戳。已有来源、重新投递旧 ID 或旧 reserved 集合不能借此进入试验。

主 Dot 读取并核验精确新消息 ID、本人身份、正文 hash 与完整入站清单，再调用 `select_weixin_dot_single_source_trial`，使用原请求 ID、消息 ID 和当前修订号。选择要求无历史 ledger，无 unknown、已授权、committed 或授权历史不明 journal。此前仅有可证明从未授权的 prepared 不等于获得发送权，仍须按正式协议处理。

选择只能原子确定一个来源与固定 scope。批量 `state=closed`、`new_admission_enabled=false` 仍保持不变；单条许可需看 `single_source_admission_enabled`、`any_new_admission_enabled` 和 `single_source_trial.selection_active`。

本协议只能登记一次。响应丢失重读同一决定，不能换请求、标记、来源或 scope。登记后批量 enable 永久拒绝；不得删除 trial 或触发器来重开，扩展常规准入需要另行审查协议。

## 每部分的发送与证据

按当前 schema 对稳定 journal key 执行 prepare → 一次 authorize → 一次对应 Dot 写入 → commit 原目标 ID → 精确目标读回。对来源自身的 expected parts 分别核对原文和实际 final，不扫描整段无关历史来决定新许可。

任一 authorize 回包丢失、远端响应未知或发送后退出，都按可能已发送处理。保留原身份、许可和 unknown，以正向目标关联恢复；不得再 authorize、换 attempt 或重发。commit 成功也只证明回执已记录，必须核对同 scope 的完整原文/回答与 metadata 才能验收该部分。

简单短文只回复一条 final，不为了测试增加 ACK。微信回复按自身稳定槽位独立处理；Dot 阻塞不能让其自动重发。微信 accepted 与手机实际显示分开记录，取得本人确认后才报告收件。

## 结束与结论

结束时执行新的正式 close 意图并读回 `any_new_admission_enabled=false`、`selection_active=false` 与当前修订号；重启不是关闭。close 不能撤销已有 permit 或未知远端发送；逐个按原 journal/attempt 对账未决许可，保留 unknown，不宣称后续远端写入已不可能。

记录实际代码版本、开始/结束时间、授权范围、各部分状态、精确读回结果、手机确认以及剩余 unknown；公开报告去除真实消息、账号和私有目标标识。各段时延明确观测起止点，不能把 webhook accepted 到首次读取全归因某一组件，也不承诺三秒手机显示。

一条短文通过仅证明这条路径在该次观测成立；附件、多轮持续处理、平台调度时延与远端严格恰好一次仍需各自证据。兼容回退保留本次全部状态，详见 [运行与恢复](operations.md)。

本版在单条 trial 后不能直接开启长期镜像：bulk enable 永久拒绝，close 后没有第二条来源许可。源码另含 `docs/STEADY-FUTURE-ADMISSION.md` 的新候选；只有经单独审查部署与持续准入授权后才可使用，不能删表或恢复旧库绕过。
