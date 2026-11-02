# 先验证 MCP 到达新进程

此入口只解决基础传输诊断。它不导入或构造 Probe，不打开任何 runtime 数据库、凭据或二维码，不轮询微信、不访问网络、不启动子进程、不接受订阅，也不生成批准记录。

默认只显示计划，无实例标识和测试文字：

```sh
python scripts/connection-probe.py
```

之后只有在官方客户端运行条件和原私有 Tunnel 访问已经获准时，才可把以下命令用作临时 stdio 子进程。本命令不自行安装、启动或配置 Tunnel：

```sh
python scripts/connection-probe.py --stdio --duration-seconds 60
```

默认只接受 `initialize`、`server/discover`、`ping`、`tools/list` 和空参数的 `get_weixin_status`；标准对象型 `_meta` 可携带但完全忽略，不回显、不给授权、不得变成工具参数。通知无写入效果。所有微信正文／附件读取、发送、ACK、登录和事件方法默认被拒绝，错误不回显请求正文。默认及最大运行期分别60秒和180秒；stdin EOF、期限、消息长度或请求数达到上限时退出。stdout／stderr直接使用带截止时间的非阻塞字节写入，不留下解释器退出时可能再次阻塞的TextIO缓冲；输出端不读取也必须按期退出。入口适用于POSIX管道。

`get_weixin_status` 复用原工具身份，但返回明确的 `read_only_mcp_connection_check` 诊断：`mcp_request_received=true`，`binding=not_read`，`weixin_authentication=not_checked`，不返回可误认为微信已连通的 `connected` 值。它只证明本次 MCP 请求抵达该诊断进程，不能证明微信绑定、认证、手机收到、完整DOT接线或唯一客户端。

进程启动时在stderr写入一条非敏感随机 `instance_id`；工具返回同值，便于操作者比较实际新机启动与远端请求。值不是凭据，不含机名、序列号、路径或账号。进程重启会产生新值；它不是跨机器锁或服务端接管证明。其余stdout只包含MCP JSON-RPC响应，不记录原请求或秘密。

## 和订阅的边界

基础发现不要求事先取得订阅ID、请求摘要或callback主机。默认行为不变；只有已经核实原私有Tunnel传输范围、并获准观察真实订阅请求时，才可显式使用：

```sh
python scripts/connection-probe.py --stdio --observe-subscriptions --duration-seconds 60
```

仅给 `--observe-subscriptions` 而不给 `--stdio` 时仍只显示计划。观察模式声明events能力，`events/list`只列 `weixin.message`；收到有合法请求ID的 `events/subscribe` 后仍返回错误，不执行callback challenge、不注册／刷新／取消订阅、不访问网络或runtime。不带ID的通知没有观察或写入效果。

形状合法的请求只在错误响应和stderr中留下固定字段：`observed_not_authorized=true`、`candidate_callback_host_digest`、`candidate_route_digest`、`candidate_subscription_id`及`requested_lifetime`。TTL区分省略、null和有限整数的精确值。完整URL、hostname、签名secret、cursor、正文及额外元数据均不输出；即使请求方把敏感值编码进合法hostname，也只输出其摘要。摘要按原Probe订阅身份算法生成；它是候选身份，不能证明调用方、原订阅或唯一客户端。观察结果不是批准记录，不能自动转换或写成批准文件。

本轮只消除了必须预先固定含可轮换secret／cursor的完整请求这一不兼容条件；后续[有限验收入口](BOUNDED-DOT-STDIO.md)可显式选择稳定路由匹配。**观察仍不能独立完成bootstrap。** 精确callback主机必须另从可信平台配置或获准私有核验取得；仅有host摘要不授权推测／扩大允许列表。原调用是否会在有限窗口发送subscribe也未保证，不能伪造请求或改变持续订阅来制造证据。

此诊断默认只宣告tools能力，观察选项才额外宣告events，且始终只列一个诊断状态工具。**不要使用任何临时能力集重新扫描、发布或更新原App／插件。** 临时目录变化或拒绝一次真实subscribe对远端续订／缓存的影响尚未验证；本地零写不能证明原持续订阅无损。诊断本身不修改、取消或维护任何订阅。将原Tunnel指向本入口属于实际传输配置动作，必须另核范围和影响，不能因本地测试通过自动执行。

上层客户端或supervisor可能重启退出的stdio子进程，因此本进程期限不证明整个Tunnel进程也已停止。实际临时连接必须采用已确认的有限前台生命周期；不能注册常驻、使用后台保活或假称本脚本能关闭另一进程。

取得真实MCP到达证据后，再走项目原有的订阅验证、本人一条新消息、原message_id和send_id防重、手机收件验收。这个入口不自动切换到收发模式，不证明最终产品已可用。
