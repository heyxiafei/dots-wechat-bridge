# Codex Pocket Connector 设计对照

本轮仅优化原微信桥候选，不安装或运行参考项目、不接入其桌面接口。参考仓库是 [a110q/Codex-Pocket-Connector](https://github.com/a110q/Codex-Pocket-Connector)，审查固定于 commit `5a613ca875baf755db79eb7f6f30b78bcb498239`。

已读取的来源：[standalone-adapter.mjs](https://github.com/a110q/Codex-Pocket-Connector/blob/5a613ca875baf755db79eb7f6f30b78bcb498239/standalone-adapter.mjs)、[protocol.mjs](https://github.com/a110q/Codex-Pocket-Connector/blob/5a613ca875baf755db79eb7f6f30b78bcb498239/protocol.mjs)、[approvals.mjs](https://github.com/a110q/Codex-Pocket-Connector/blob/5a613ca875baf755db79eb7f6f30b78bcb498239/approvals.mjs)、[companion.mjs](https://github.com/a110q/Codex-Pocket-Connector/blob/5a613ca875baf755db79eb7f6f30b78bcb498239/companion.mjs)。[许可证](https://github.com/a110q/Codex-Pocket-Connector/blob/5a613ca875baf755db79eb7f6f30b78bcb498239/LICENSE)为 MIT，Copyright (c) 2026 久安。本次借鉴边界设计，Python 实现为本项目独立编写，没有复制或翻译其源码；若以后复制实质代码，须保留其完整版权和许可声明。

## 功能差异与取舍

|参考设计与实际来源|本项目已有能力/限制|本轮处理|
|---|---|---|
|`standalone-adapter.mjs` 的 `withThread` 串行化；发现已有 owner 后跟随该 writer，发送超时查询原消息回执|本地 coordinator flock、串行提交、持久 claim 和可信宿主 permit；这些不能证明另一机器/云任务没有 writer|保留原机制。没有 dot 云端公开 owner-discovery/接管接口证据，不移植桌面 IPC，不启动第二 writer。|
|`companion.mjs` 的 CommandLedger 用原 command ID/参数摘要查状态，未知结果不重放|原 text/media 独立稳定键、claimed/unknown/denied 持久记录、原 send_id 查询建议和既有 get_weixin_send_status|保留更严格的原拒绝/未知规则。不采用按错误字符串将 uncertain 降为“必未提交”的兼容迁移；no_reply 不证明未执行。|
|`protocol.mjs` 在进入 command ledger 前校验外层参数|微信传输已限制非空 UTF-8 1..8192 bytes；协调入口原只验非空，过长文本可能先 claim 再失败|增加协调入口同上限预检，在新 claim/permit/发送前拒绝无效文本；不截断、分片或换 ID。原账本/已承诺槽优先，不因新坏文本掩盖历史未知/拒绝。|
|`approvals.mjs` 将响应限制于原请求提供的选项与请求权限子集|本地附件契约已绑定 owner/request/artifact/attachment/hash/channel，原漏绑 kind 和模型原文件选项|补齐 kind 与可信 scope 一致；model_source 原文件必须 item 与 scope 的 original_requested 都是 literal True。仍由原平台审批，不能把普通聊天或本地标志当批准。|
|`standalone-adapter.mjs` 按 threadId/requestId 找 pending 请求；断开或 resolved 清理请求|当前 dot 宿主没有受支持的待审批请求管理入口；permit 每次写前重新核验现有 writer/本人范围|不造审批转发器或本地批准列表。额外修复 permit 抛出真实 PlatformDenied 时持久停止原动作；False/缺能力只是等待，不能伪称已获准或已拒。|
|`companion.mjs` 分开传输连接与桌面 ready；异步结果检查原 socket/channel；外层 sentAt 拒绝过期请求|微信 auth_required/context_required/未知发送已有分支；r6 本地诊断已分开 loopback 快照与线上 host；真实事件/工具关联仍需平台信息|不移植新的连接/加密/重连栈，不自设微信授权 TTL。参考 protocol 的五分钟仅针对远程 command envelope，不是本人请求或微信 context 的固定有效期。|

## 明确保留的边界

- 不使用参考项目的私有桌面 IPC、state_5.sqlite、会话文件、APK、第三方中继或 full-access 模式；没有读取本机 Codex sessions、安装依赖或执行其脚本。
- 现有受支持认证与传输保持；没有新增密钥体系、持久凭据、监听端口、触发器、订阅或权限。
- 固定 ACK 关闭，旧 dot 镜像只读；被拒通知不追发。普通回复与额外通知仍分别核验范围。
- 真实审批只来自当前受支持的原请求/宿主；不能复制批准、扩大选项、改工具或换身份重试。候选范围检查返回 authorization_granted=false。
- API accepted 与手机收到分开。以上设计不能证明更快、所有操作同会话、自动上线或免审核。

## 离线验证与升级边界

针对新增缺口的回归在 `test_submission_preflight.py` 和 `test_attachment_scope_boundaries.py`，使用临时账本与明确 fake 工具。完整项目套件及 schema 仍按 AGENTS 要求由根任务统一验证。

原 R7 最小补丁不改变数据库 schema、原 action key/digest 算法、工具 schema 或宿主构造参数。公开整合版另有 AIHot 可信配置注入与对应工具 schema 调整，须按本发行清单和安装接续说明独立验证。r6→本候选→r6 的合成状态演练用于确认拒绝/未知/已提交记录保留，不能替代已安装 r4 或真实线上回滚验证。回退到 r6 会恢复旧的文本预检和附件选项漏洞；因此不能把格式兼容等同于建议生产回退。

实际在线 client/host/service、完整原 subscription 与当前协调任务的关联，以及官方额度 reader、账户绑定、原生受审批工具与持久账本接线仍需平台证据。候选只提供实现，不会赋予当前云任务这些能力。正式部署、扫码、真实手机收发及重启恢复仍需独立验收。
