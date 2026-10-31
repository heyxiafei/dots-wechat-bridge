# 新安装的一条全新微信消息验收

这个入口只验证已授权新机器与本人微信之间的传输。它不运行 Tunnel，不启动常驻服务、MCP、事件泵、媒体处理或通用发送线程，不证明云端 dot 已接通。原持续订阅不修改，旧机器的消息和历史库不读取、不迁移。

先完成安装预检；已有扫码绑定不重复扫码。实际运行前，操作者必须在可信本人上下文中确认本轮测试授权，以及旧发送者确实停止（例如本人明确确认旧 Mac 在验收期间保持关机）。本机 worker.lock 只能隔离这个目录；命令中的两个 reference 是审计引用，不能证明旧机已停止，也不授予权限。

默认只生成预览，不打开状态库或连接网络：

```sh
.venv/bin/python scripts/check-new-message.py
```

在还不具备发送者隔离条件时，可以只核验新绑定的官方配置读取：

```sh
.venv/bin/python scripts/check-new-message.py --check-config \
  --state-dir /absolute/new/install/runtime/state
```

它以只读 SQLite 访问本安装绑定，正常认证只在客户端内部使用，对官方 `/ilink/bot/getconfig` 进行一次最长 10 秒的请求（CLI 总计 12 秒界限），不使用 getupdates、发送、扫码或订阅。只有 HTTP 成功后严格整数 `ret=0` 且无冲突错误字段才标 verified；缺失或异常状态保留 unknown。typing_ticket、完整响应、owner、token、base URL 不输出或保存。这个读取不启动第二个 writer，因此不需要先停止旧发送者；也不证明 MCP 或手机收到。

准备好后，使用预览的 round_id 和真实授权/隔离证据引用。以下占位符不可当批准：

```sh
.venv/bin/python scripts/check-new-message.py --live \
  --state-dir /absolute/new/install/runtime/state \
  --round-id '<ROUND_ID_FROM_PLAN>' \
  --approval-reference '<ACTUAL_OWNER_REQUEST_REFERENCE>' \
  --isolation-reference '<ACTUAL_OLD_WRITER_STOP_EVIDENCE>'
```

入口默认等待 120 秒，最多 180 秒；打印本轮随机测试短语后，本人在手机发送该短语一次。只允许唯一 ready 绑定，原账号 cursor 必须为空，且所有业务入站、出站、订阅和其他非诊断状态都必须为空；遇到已有数据保持原样并拒绝。它不是删除历史或跳过生产 retained_state 检查的方法。

过滤首先只看 owner、接收账号、真实 message_id 和严格时间窗。官方 to_user_id 是可选字段，允许缺失、空值或本 bot，显式其他 bot 必须拒绝。历史或其他来源的正文不检查、不存储、不输出。候选必须是一个精确匹配随机短语的纯文本 item，具有本条消息的 context_token，且没有已知引用/转发标记。文本 item 只兼容官方有限的 create_time_ms/update_time_ms/is_completed/msg_id 元数据，其余载荷拒绝。字段依据 [腾讯固定版本类型定义](https://github.com/Tencent/openclaw-weixin/blob/874637a0dbc24050f72d1385add4fc6170a2d726/src/api/types.ts)。协议没有完整的转发来源证明，因此缺少标记不等于已证明“原发”。若一个批次包含两个不同 source 的匹配短语，直接停止，不任选一条。

不会保存或推进 get_updates_buf；若一个非空批次没有本轮匹配消息，就停止，不为追到较新的消息而读取历史积压。请求 getupdates 的上游行为由协议决定，不能声称它对远端队列绝无影响；这里保证不迁移历史、不推进本地或内存 cursor、不重放旧回复。

只有匹配的新测试短语和一个正常 final 写入新库：使用原项目同样的本地 message ID、final send_id 和 client_id。发送前必须持久提交 sending、attempts=1，紧邻 POST 再核绑定及过期；至多发一次 POST。attempts 是持久提交标记，不是已发生网络请求的证明：若提交后过期，仍会保留不可重发的终态而零 POST。所有明确非零响应（包括限速、context 或 auth 错误）在本诊断中为终态 rejected；超时、HTTP/解析错误、异常结果或进程中断为 unknown，不自动重试。API 返回空对象或省略成功码沿用现有协议的 accepted 语义，不等于手机收到。原 outbox 和诊断回执不一致时按 unknown 停止，不误报 accepted。

同一目录只允许记录一个诊断轮次。重启/重复执行只读取原回执，不重新等待、换短语或发送。需要另一次测试时先由操作者审查原结果和新授权；本入口没有自动清空或 rearm 选项。

查询原本地回执：

```sh
.venv/bin/python scripts/check-new-message.py --receipt \
  --state-dir /absolute/new/install/runtime/state \
  --round-id '<ORIGINAL_ROUND_ID>'
```

这不是腾讯远端 send_id 查询接口；现有 IlinkClient 没有该能力。回执不会包含 token、账号/owner ID、网址、原始响应、真实非测试正文或附件信息。手机收到必须由本人另行明确确认。结束后没有常驻接收或发送进程；不自动切生产或注册后台任务。
