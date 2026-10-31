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

只接受 `initialize`、`server/discover`、`ping`、`tools/list` 和空参数的 `get_weixin_status`；标准对象型 `_meta` 可携带但完全忽略，不回显、不给授权、不得变成工具参数。通知无写入效果。所有微信正文／附件读取、发送、ACK、登录和事件方法被拒绝，错误不回显请求正文。默认及最大运行期分别60秒和180秒；stdin EOF、期限、消息长度或请求数达到上限时退出。stdout／stderr直接使用带截止时间的非阻塞字节写入，不留下解释器退出时可能再次阻塞的TextIO缓冲；输出端不读取也必须按期退出。入口适用于POSIX管道。

`get_weixin_status` 复用原工具身份，但返回明确的 `read_only_mcp_connection_check` 诊断：`mcp_request_received=true`，`binding=not_read`，`weixin_authentication=not_checked`，不返回可误认为微信已连通的 `connected` 值。它只证明本次 MCP 请求抵达该诊断进程，不能证明微信绑定、认证、手机收到、完整DOT接线或唯一客户端。

进程启动时在stderr写入一条非敏感随机 `instance_id`；工具返回同值，便于操作者比较实际新机启动与远端请求。值不是凭据，不含机名、序列号、路径或账号。进程重启会产生新值；它不是跨机器锁或服务端接管证明。其余stdout只包含MCP JSON-RPC响应，不记录原请求或秘密。

## 和订阅的边界

基础发现不再要求事先取得订阅ID、请求摘要或callback主机；生产及有限发送入口的原门槛不变。**本入口只拆开基础MCP连接步骤，没有解决完整订阅恢复循环。** 因为所有events方法都被拒绝，它既不会取得原订阅请求摘要，也不会完成callback握手。后续仍须先明确平台原订阅的实际续期／恢复路径，并单独审查如何在受信原连接上验证真实请求；不得把本次连接诊断成功当成摘要已取得或有限收发可以启动。

此诊断只宣告tools能力，不宣告events，且只列一个诊断状态工具。**不要使用这个临时能力集重新扫描、发布或更新原App／插件。** 平台是否会因临时缺少事件能力而影响既有订阅尚未验证，不能据本脚本声称原持续订阅在切换期间必然无损。诊断本身不修改、取消或维护任何订阅；`events/subscribe`／`events/unsubscribe`均拒绝。将原Tunnel指向本入口属于实际传输配置动作，必须另核范围和影响，不能因本地测试通过自动执行。

上层客户端或supervisor可能重启退出的stdio子进程，因此本进程期限不证明整个Tunnel进程也已停止。实际临时连接必须采用已确认的有限前台生命周期；不能注册常驻、使用后台保活或假称本脚本能关闭另一进程。

取得真实MCP到达证据后，再走项目原有的订阅验证、本人一条新消息、原message_id和send_id防重、手机收件验收。这个入口不自动切换到收发模式，不证明最终产品已可用。
