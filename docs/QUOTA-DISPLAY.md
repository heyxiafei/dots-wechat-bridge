# 可复用的 Codex 额度适配与展示

`probe/usage_display.py` 是可执行的纯 Python 模块，供自然语言主处理者使用。它适配已取得的官方只读结果，生成统一中文 emoji 文本；没有采集器、MCP工具、聊天命令、网络、凭据读取、缓存、定时任务或发送器。只安装当前微信桥不会自动获得官方额度查询能力。部署状态以本次维护记录为准；格式支持不等于真实查询已接通。

候选中的 `OwnerCoordinator.handle_fixed_command` / `handle_fixed_command_async` 另提供“额度／查额度／查一下额度”到已注入官方 reader、四行结果和原 final 槽的完整调用入口；它不是自动安装的事件钩子。只由受信任宿主在核实本人意图与账户后调用，具体参数与发送边界见[协调者接线](COORDINATOR-WIRING.md)。`fixed_command` 仍为建议，不能从提示或微信正文提取授权。

## 已有官方契约与明确未接通项

窗口字段依据[官方 Codex App Server 文档](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt)：`rateLimitsByLimitId` 是按额度桶区分的结果，`rateLimits` 是 legacy 单桶视图；桶内 `primary/secondary` 的实际 `windowDurationMins` 决定窗口名称，`usedPercent` 是已用比例，`resetsAt` 是 Unix 秒。不能把 primary 固定当五小时，或把 secondary 固定当周窗口。

某些 Codex 任务提供只读 `get_usage_limits`。桥接 Python worker 本身没有调用该工具的入口，也没有内置 `account/rateLimits/read` 客户端。文档存在该读方法不等于当前桥已获得安全认证和调用能力；本模块不通过CLI、秘密文件或后台接口补采集器。

官方说明提到 credits，但本次没有取得可固定依赖的余额子字段完整输出schema。因此 `CreditInfo` 是**显式的协调者映射契约**，不是声称每个工具必定返回 `credits.balance/unlimited`。只有主处理者核实其实际来源和字段映射后，才能设置 `mapping_verified=True`；否则显示未知。`hasCredits`、重置券的 `availableCount` 和窗口百分比均不能充当余额。

## 调用方契约

1. 先按原回复流程读取本人自然语言请求、检查 `reply_state`。已有final时保留原文字和ID，不重新查询后改写已承诺回答。
2. 在可信协调上下文确认当前微信owner与实际官方读取账户对应，构造 `UsageBinding(owner_ref, account_ref, verified=True)`。这些是不显示给微信的非秘密、不透明引用；不能从聊天正文/用户JSON接受verified标志，也不能把其他安装者或原作者的账户当默认值。
3. 检查当前任务是否有获准的官方只读能力。若无，`read_and_format_usage(None, binding)` 明确返回未接通；不能返回演示值、旧快照或零额度。
4. 若有，主处理者在读取前后核对同一账户上下文，沿用该环境的官方只读工具。由主处理者记录本次完成时间，构造 `UsageObservation(owner_ref, account_ref, read_at, payload, source='get_usage_limits', credits=...)`。
5. 调用 `render_usage`，或提供受信任reader回调给 `read_and_format_usage`。回调最多调用一次，不自动重试，不负责发微信。最后主处理者按原消息ID使用既有幂等final槽回复；本模块不提交任何发送。

`UsageBinding.verified` 是调用者提供的证明前提，不是本模块执行OAuth/账号认证的结果。结果中未必包含账号ID，因此仅比较引用不能自行证明账户归属；无法在可信主处理层核实，就保持未核验，不展示他人结果。模块不会输出这些引用。

## 可用接口

```python
from probe.usage_display import (
    UsageBinding, UsageObservation, CreditInfo,
    normalize_usage, format_usage, render_usage, read_and_format_usage,
)

# expected_owner/account_ref 必须来自已核实的协调上下文，不是用户消息。
binding = UsageBinding(expected_owner, expected_account, verified=True)

# 本模块没有默认reader；None会明确返回“未接通、当前额度未知”。
not_connected = read_and_format_usage(None, binding)

# official_result/read_completed_at由获准的官方读取步骤提供，不由此模块获取。
observation = UsageObservation(
    owner_ref=expected_owner,
    account_ref=expected_account,
    read_at=read_completed_at,
    payload=official_result,
)
reply = render_usage(observation, binding, now=current_time)
assert len(reply.text.encode('utf-8')) <= 8192

# 仅当实际字段映射已核实才提供余额；不要凭示例猜键名。
credit_info = CreditInfo(balance=verified_balance, mapping_verified=True,
                         scope='workspace')
```

支持直接结果Mapping、JSON-RPC result、MCP structuredContent，以及单个有界JSON文本块（含只有 `content:[{type:"text",text:"…JSON…"}]`、无 structuredContent 的官方工具返回）。structuredContent/result 包装优先于同层额度字段；错误包装不能被同层旧数值遮盖。多文本块、无效JSON、超64 KiB文本及过深包装不会被拼接或猜测。未支持的包装或错误响应返回未知，不回显原始结果/异常。`accountId:null` 不提供账户对应证据；仍需要宿主的独立绑定证明。

## 数据和展示规则

周额度专用`fixed_commands.prepare_weekly_usage`保留约定四行。工具缺失/reader非可调用时status=unavailable、official_call_attempted=false，并写明“未接通，未调用”；账户未核验不调用；reader实际抛错才为read_failed并写明官方读取失败。状态解释置于第一行，不新增第五行。这个attempted字段只记录注入回调是否实际被调用，不能证明回调就是官方工具；官方来源仍需可信协调者的真实回执。

- 默认仅选择公开额度桶标识 `codex`；调用方可显式选择另一安全标识。不同桶不求和、不互相借用credits；map存在但为空、缺目标桶或非null损坏时，不静默退回legacy。map缺失/null时可使用legacy，记录来源差异。
- 300分钟才称5小时，10080分钟才称周窗口；其他正整数时长如实命名，时长缺失标未知；重复时长保留primary/secondary区分，不合并。
- 有效数值按 `remaining=clamp(100-usedPercent,0,100)` 展示；超100%的已用值保留并标明接口异常范围，负数、bool、NaN、Infinity、非数字等保持未知。缺失窗口不等于已用0或剩余100。
- 时间统一显示北京时间及日期，不把毫秒猜成秒。未提供或无效的重置时间标未知；重置时间已过时隐藏旧用量，要求重新读取，不自动恢复100%。
- read_at由可信读取步骤提供。默认300秒过期阈值是项目自身策略，不是官方TTL；旧快照只显示旧读取时间和“当前未知”，不展示旧余额/百分比冒充当前。未来或无效读取时间同样拒绝。
- 显式核实的余额可显示0、非负余额或不限额标记；未核实、缺失、异常时显示未知，工作区余额保留范围标记。不重置额度、不充值。
- 无跨用户缓存或私有账户配置；不存原始结果，不访问文件或网络。所有文本<=8192 UTF-8字节，沿用现有回复限额。

## 接入范围与后续条件

候选已实现结果适配、格式化、provider 和受信任宿主固定命令分派。handler 在读取额度前与提交前检查原 ack/final 和本地持久记录；已有提交/unknown/拒绝按原记录处理，不重新读新数值覆盖旧回答。同消息 sync/async 调用共用短期处理标记，不持线程锁跨 await；取消只释放尚未提交的处理标记。原生发送仍经过当前宿主 permit，unknown 不重发。

端到端尚未接通，未在真实查询/微信发送中验收：需要原协调任务的受支持执行环境实际提供该使用者自己的官方只读工具、可信账户映射和原生受审批工具适配器，再将事件处理接到入口。若该环境无法运行本Python模块，仍需受支持的调用方式；不能声称远程MCP安装后会自动调用本地函数。新 Work 的工具或一次真实读取不能证明旧云端协调任务拥有同样能力。

本次不新增server工具，不更改schema、触发器、菜单、ACK、订阅、幂等发送器、权限或服务。菜单、长图与额度按本次明确授权成组维护；不因此获得发布、新凭据或更大访问范围。
