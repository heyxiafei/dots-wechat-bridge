# DoT / GPT Work 宿主后端

`foundation.host_backends` 提供实际的提交、关联、查询和错误处理代码。它不注册云端工具，不创建第二个微信处理者，也不持有微信登录凭据。默认 `DoTBackend()` 和 `CloudWorkBackend()` 的健康状态均为 **BLOCKED**。

代码已用合成宿主验证；本候选尚未接入真实 DoT 或 Work，也没有恢复微信额度读取。现有生产 DoT 的身份、任务、工具、记忆和唯一微信回源路径继续保留。

## DoT 接口

```python
from foundation.host_backends import DoTBackend, HostBinding

backend = DoTBackend()  # BLOCKED / host_entry_not_injected
```

可信宿主需要注入：

- `submit_task(request, timeout_seconds=...) -> BackendResult`：使用真实已授权的 DoT 任务入口，只返回数据；不得自行发微信。
- `HostBinding(thread_id, host_id, proof_ref, verified=True)`：路由由正式宿主证据核实；这些值不能从模型回复复制。
- `bounded_submit_verified=True`：宿主已经证明实际调用支持传入的等待预算。同步 Python 不能打断一个阻塞的 callable；本模块没有通过线程或后台进程假造超时。

只有这些条件齐备，`health()` 才返回 READY。READY 表示注入契约齐备，仍不表示生产已接通或手机验收通过。模块不查找认证文件、账户或 SDK，也不把普通 API 模型冒称原 DoT。

同一对象内，首次调用前即记录 request，重复提交不会再次调用宿主；相同 request_id 的不同正文或关联信息返回 `request_conflict`。异常一律固定错误码，不包含异常原文。明确权限拒绝后，该对象停止新的提交。迟到结果不返回正文，不触发第二条微信回复。

附件、任务查询和取消当前不支持；不能因 DoT 产品本身可能有这些能力，就替这个适配器打开能力声明。`submit_task` 不能把收到的附件或正文当作权限证明。

## Work 接口：正式入口的规范化边界

目前核实的云端工具契约子集是：正式发送接受 threadId/prompt 并返回 admitted turnId；正式读取返回 latestTurn.id/status 与新到旧 items（id/role/text/turnId）。状态可能滞后，读取受授权限制。该事实不足以证明终态、final、完整性、宿主和有界等待。

因此本代码**没有猜测原始 wire envelope**。真实宿主需要把实际工具结果规范化为下列类型，并提供已核实的 `WorkContract`：

```text
transport.admit(thread_id, prompt, timeout_seconds) -> Admission(turn_id)
transport.read(thread_id, timeout_seconds) -> ThreadSnapshot(
    thread_id, host_id, latest_turn_id, latest_status,
    items=(WorkItem(item_id, role, text, turn_id), ...),
    complete=..., execution_verified=...)
```

`Admission.turn_id` 必须取正式发送回执。`host_id`、`complete` 和 `execution_verified` 必须由可信平台/宿主的路由与执行证据核实；它们不是现有 cloud wire 契约已经存在的字段，也不能由子端 JSON 自证。

`WorkContract` 默认所有验证标志均为 False。宿主必须明确提供：路由绑定，互不重叠的成功/失败/处理中状态，final 规则及出处，真实有界调用能力，以及 data-only 执行边界。当前支持的 final 规则为 `newest_assistant_is_final`，只有平台确实保证“成功终态中最新 assistant 项是最终答复”时才可启用。没有这些真实证据，保持 BLOCKED，不为演示而把标志改 True。

## Work 的执行与回执

`submit` 一次受理后返回 `pending`，task_id 是正式 admitted turn；这不代表已经执行完成。`status(task_id)` 只读取该目标 thread，最多进行一次有界观察，不内置无限轮询。它始终保留原 request_id：

- latestTurn.id 不等于 admitted turn 时，结果未知；不能把另一轮状态套用到本次。
- 处理中状态只返回 pending；即使正文已经出现，也不提前当作 final。
- 成功终态还需要完整、已核来源的快照；按已核规则选取该 turn 的最新 assistant 项。
- 重复 item ID、未知状态、错误宿主、歧义/不完整记录、JSON 重复键、额外字段或 Markdown 围栏均不形成成功结果。
- 发送结果未知不再次派发。读取拒绝不会被重分类成“任务未执行”；明确拒绝后停止后续提交。
- deadline 从原提交时固定，重复查询不续期。过期只结束观察，结果未知；不取消原任务、不解除外部账本、不重新读取额度。
- 同一对象的并发 status 查询不会启动平行观察；取消当前明确返回 unsupported。

Work 任务最终文本应为唯一的下列业务 JSON，不能有前后话。它只有数据权限；真实来源还要依靠可信外层执行记录，`data_only` 本身不授予或证明权限。

```json
{
  "kind": "bridge_backend_result_v1",
  "version": 1,
  "data_only": true,
  "can_send": false,
  "request_id": "example-request",
  "message_id": "example-message",
  "status": "succeeded",
  "text": "示例结果",
  "backend_session": null,
  "error_code": null,
  "usage": {}
}
```

允许的最终业务状态为 succeeded、failed、unsupported。非成功状态 text 必须为 null。用量只接受受限非负整数；未返回的值不补零。这个通用任务回执不替换既有的官方额度20字段回执，也不证明 get_usage_limits 实际被调用。

## 持久性、并发和真实接线条件

适配器的 request/turn 缓存及拒绝标记只存在于对象生命周期内，不是跨进程幂等账本。原始持久请求、路由冻结、重启后的未决/未知隔离、发送记录和权限停止应由桥接核心及真实宿主管理。重启后不能重建适配器并重新提交旧任务来“恢复”；当前实现没有跨进程操作重附着功能。

同一 Work 忙碌时，实际平台是排队新 turn、拒绝还是向现有 turn 追加消息，仍须正式核实。只读 latestTurn 检查不是原子锁；本模块不声称排他控制其它任务写入者。若两个请求被返回同一 admitted turn，后一个不会被重新绑定到前一个请求。

真实接线仍需：原 DoT 实际注册的正式工具、指定新 Mac 路由证明、终态/final/分页完整性保证、同 turn 的工具执行证据和真正的调用预算。这些条件齐备后，先用无敏感标记验证正式往返，再用新的本人微信消息验证实际读取与唯一手机收件。已回复消息不重试；此前 AppServer 的权限拒绝不会因导入这个模块而解除。

## 本地验证

```sh
python -B -m unittest foundation_tests.test_host_backends
```

所有测试使用假 callable、假 transport 和合成数据，无真实任务、额度请求或微信发送。父层集成仍应调用唯一原发送器，API accepted 与手机收到分别记录。
