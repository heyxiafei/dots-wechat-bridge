# 单条新消息的完整 DOT 验收候选

本入口是候选代码，尚未部署或进行手机验收。它直接组合已有 Probe、MCP JSON-RPC／Events 和腾讯 iLink 客户端，不新增宿主 API，不以本地回声代替 DOT 回答。

默认仅显示计划，不生成二维码、批准记录或可误认为已经开启的测试文字：

```sh
python scripts/bounded-dot-stdio.py
```

## 运行条件与授权边界

外部须先核实官方客户端获系统接受、原私有 Tunnel 运行权限、准确原平台订阅身份与本轮本人授权，再准备私有批准记录。JSON 中证据引用只是审计记录，不授予权限或证明跨机器隔离；不得从普通微信正文、观察输出、模型自拟字符串或示例自动生成批准。

记录须属于当前用户、无符号链接、仅本人可读写，包含：

|字段|实际来源|
|---|---|
|`round_id`|本轮准备时生成的32位十六进制随机值|
|`duration_seconds`|1–180秒，结束不自动转常驻|
|`valid_until`|开始前未来最多300秒的UTC epoch秒|
|`approval_reference`|本人对此有限真实验收的具体授权|
|`accepted_transport_evidence_reference`|实际可运行客户端及原Tunnel接线证据|
|`private_tunnel_access_reference`|原私有Tunnel当前受支持访问授权|
|`platform_request_evidence_reference`|平台原事件订阅请求可信来源记录|
|`binding_sha256`|本次ready绑定及代际的实际摘要，复用已有binding检查|
|`subscription_id`|平台原请求对应的真实原订阅ID|
|`subscription_request_digest`|旧exact批准专用：实际原events/subscribe params按既有canonical编码的SHA256；不展示或复制其中秘密。稳定路由模式禁止带此字段|
|`callback_host`|原请求中的精确、已核回调主机；不接受通配符|

缺真实来源即保持未运行。不自造请求、复制旧库秘密或创建替代订阅。应通过原受支持传输观察真实平台握手，不能把尚未收到请求直接判断为平台必须手工重新交付。新机数据库须只有已扫码绑定和原初始化空结构，不读取或迁移旧历史。

### 明确选择稳定路由匹配

旧批准不带模式字段、只带完整 `subscription_request_digest` 时仍按原完整请求严格匹配，不自动升级。需要容许官方协议中的secret／cursor轮换时，必须重新核实具体授权，显式设置 `subscription_match_mode="stable_route_v1"`，移除旧完整请求摘要，并提供以下字段：

|字段|校验|
|---|---|
|`subscription_route_digest`|原完整callback URL、固定 `weixin.message`、空arguments及既有私有主体标记的canonical列表SHA256；和原Probe算法一致，不规范化URL或仅比较host|
|`callback_host_digest`|人工核实的精确 `callback_host` 的SHA256，且须与收到请求的host摘要一致|
|`subscription_id`|已核原ID，须同时等于完整路由摘要前32位所构成的既有 `sub_` ID|
|`subscription_lifetime`|精确TTL语义：`{"kind":"omitted"}`、`{"kind":"null"}`或`{"kind":"finite","value":180000}`；有限值仅示例，必须来自真实授权|

缺mode的新字段、未知／null模式、旧新混用、损坏字段均拒绝。TTL省略与显式null不是一回事；省略走原Probe默认86400000毫秒，null明确请求无期限，有限值继续沿用原Probe的1000–86400000毫秒授予边界。新请求必须保持批准中的精确存在性／类型／数值，不能趁secret轮换扩大持续期。secret、cursor和对象型 `_meta` 不参与稳定路由身份；它们不提供权限。secret仍由原Probe校验，callback仍须真实challenge通过。

[只读观察入口](CONNECTION-PROBE.md)仅能取得候选route／host摘要、候选ID和TTL信息；不显示完整URL、hostname或secret，不写批准文件，也不证明它属于原订阅。精确callback主机与原调用方身份仍需可信来源；在这些条件取得前不得执行握手。观察选项及其临时目录不能用于重扫原App，也不保证原持续订阅在诊断期间无损。

真正获准执行时，原Tunnel的stdio子进程才可使用：

```sh
python scripts/bounded-dot-stdio.py --live --state-dir /absolute/new-install/runtime/state --approval-file /absolute/private/approved-round.json
```

示例不代表授权；本候选没有安装配置或启动客户端。运行期核绑定和期限。平台必须发出符合所选批准模式的原events/subscribe请求，完成原Probe callback challenge；不接收其他事件、地址或订阅ID。同host的不同path／query也不是同一路由。

观察本身不触发握手。真正的有限live轮次只有在具体批准有效时才允许一次原订阅握手；stable模式先验证签名secret格式，确定性的格式拒绝不调用网络、不占用握手状态。实际challenge开始后，失败／超时进入 `subscription_unknown`，不自动重试；成功后也不在同轮自动刷新，随后才允许本轮限定的新消息轮询。生产main、原发送槽、owner核验及未知发送不重试逻辑没有因匹配方式改变。

## 执行链路

`原平台subscribe → 本人一条新nonce消息 → 严格过滤 → 原Probe入库和元数据 → 原事件回调 → DOT读取原message_id → DOT提交final → 原腾讯client一次POST → 原send_id回执`

入口单线程运行，不启普通main的收发、媒体或事件后台线程。轮询最短间隔0.5秒；只读长轮询超时继续等至截止，其余错误停止。服务端真实时间、本人、完整纯文本和随机文字全匹配才进入ingest；缺时间不降级为当前时间。旧／其他消息不入库，原cursor不推进，不排空队列。多个匹配来源或只有不匹配批次时停止。

仅公开协议发现、weixin.message、状态读取、精确消息读取、该消息的reply_weixin_message和原发送状态。拒绝ACK、通知、媒体、登录／扫码、unsubscribe、其他消息ID及无合法request ID调用。read返回原Probe完整来源、空附件和引用／捕获完整性。平台原审批继续适用。

事件至多尝试一次，未知不追投。微信final由DOT真实RPC提交；发送前持久记录原outbox槽、sending和attempts=1，然后至多一次消息POST。拒绝终止，未知不重发；重复final保留原文本／ID，不同文案显式标冲突。accepted只表示接口接受，不表示手机收到。

## 结束、查询与回滚

到期、退出或崩溃后重启不领新消息、不重发事件／回复。原轮次只读查询不要求批准仍有效：

```sh
python scripts/bounded-dot-stdio.py --receipt --state-dir /absolute/new-install/runtime/state --round-id ORIGINAL_ROUND_ID
```

回执核原outbox ID、client ID、状态、尝试数及回复摘要；sending或不一致保持unknown，不输出回复全文。WAL存在但SHM缺失时拒绝，避免只读查询创建sidecar。

持久诊断标记使新版普通main／emit在恢复队列和启动线程前拒绝此状态目录；只读status仍可用。有限验收不自动转常驻，不得删除标记或清空原账本来启动。

**写入标记后不能仅回滚旧R7源码并启动同一state：旧版本不认识标记，可能恢复诊断事件。** 必须保留state和原终态，经专门审查的受限接续／升级方案再转生产，不能覆盖数据库回滚。本轮未执行live，实际安装state未写标记；候选源码可在不触及runtime的前提下撤回。

本地nonce、worker lock和一次发送不证明跨主机唯一写入者，也不解决官方Tunnel多客户端路由限制。20秒目标、手机收件和正式上线均需真实独立证据。
