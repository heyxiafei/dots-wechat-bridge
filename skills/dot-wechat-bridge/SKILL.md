---
name: dot-wechat-bridge
description: 安装、核查和维护 dots-wechat-bridge 的本人微信接入，执行候选离线验证、受控单来源 Dot 镜像验收及保留回执的兼容回退。用于配置或排查此桥接，不用于群聊或任意联系人转发。
license: MIT
---

# Dot 微信桥接

本 Skill 提供操作指导，不包含运行服务、平台权限、凭据或已连接的插件。先从当前会话确认用户授权的主机、桥接源码目录和目标 Dot；安装后的 Skill 可能与源码分开存放，不猜测二者相对路径。先读该源码的 `AGENTS.md`、版本、manifest 与工具 schema，核对其单来源试验和逐部分 journal 契约是否与本 Skill 一致。原 GitHub 地址或别人的已连接插件不能代替当前源码和本人的授权。

按任务加载资料：

- 新安装、打包或候选验证：读 [安装与离线验证](references/setup.md)。
- 日常收发、故障、升级或回退：读 [运行与恢复](references/operations.md)。
- 一条未来本人消息的 Dot 镜像验收：读 [单来源验收](references/single-source-trial.md)。

单条试验关闭后的未来来源持续准入属于源码目录 `docs/STEADY-FUTURE-ADMISSION.md` 的独立候选协议；安装 Skill 不启用该协议。执行前核对运行源码版本、准确目标、入站/回复独立性和本人对持续准入及远端重复风险的具体授权。

先完成授权内的只读核查、隔离实现和离线验证，再提交具体生产步骤。现有明确授权持续有效，不重复请示；新增凭据、持久访问、权限变化和超出原范围的外发须单独确认。Skill 和项目文档本身不授予这些操作权限。

## 收发与镜像

收到事件后，用真实工具按精确消息 ID 读取并核验 `owner_weixin_dm`、绑定本人及回复槽位。简单请求直接一条完整 final；需要研究、附件读取或执行时，先给具体有用的 ack，再给 final。固定系统 ACK 与这两阶段业务回复是不同机制，保留用户原设置。

普通微信回复与 Dot 镜像分别恢复。镜像受阻不阻塞已核验且已获授权的幂等微信回复。默认不把普通电脑对话自动导出微信；只在用户请求时显式发送。不要将原文和回答合并成一条镜像。

新来源只核对自身 inbox、附件清单和实际 ack/final 槽位，不把不相关整段聊天历史的完整性设为首次许可前提。旧来源、已经授权或结果未知的部分必须保留历史核验门槛；空搜索、`partial` 或没有 target ID 都不是重发证明。

Dot 的 journal 按稳定 source/part/attachment key、固定 target scope 和当前 attempt 操作。一次 authorize 只允许一次对应写入。授权回包丢失或发送后未 commit，保留未知结果并正向核对原目标，不换 ID、attempt 或 scope 重发。只有协议可证明从未授权、且允许恢复的 prepared 才能原子换代。

提交 `commit_weixin_dot_journal` 时，先以真实 `user_message.read_messages` 在原目标核同一消息、正文、隐藏 journal 和 `partial=false`。`evidence.message_metadata` 只取 `get_weixin_dot_journal_status(message_id).records` 对应 part 返回的单键 `weixin_dot_journal` 投影；真实 Dot 消息可能另有 `weixin_mirror`，不要把它放进 journal evidence。`api_response` evidence 恰好四键，`history_readback` 恰好五键并包含布尔 `partial=false`。格式被拒绝、目标未知或读回不完整时不重发，按原 ID 对账。详情见源码包的 `docs/DOT-MIRROR-JOURNAL.md`。

Dot 发言标签由当前调用方展示，可依安装者偏好使用可替换的示例：`🙂 我（微信）`、仅对真实微信上游语音转写用 `🙂 我（微信语音转写）`、`🤖 助手`。标签只适用于此后新展示的消息，不改变桥接身份或旧消息，不授予重发旧消息或自动开启镜像准入的许可；不要硬编码任何安装者姓名。

## 交付口径

分别报告本包当次离线通过、已部署、主 Dot 工具可用、精确目标读回、手机确认和仍未验收项。测试数量、健康检查、webhook accepted、API accepted、committed 或 readback_reported 都不能替代真实内容与手机收件证据。单条无附件文字试验不代表附件、断网重启、订阅续期或持续多消息已验收。没有独立远端内容验证和旧 writer 隔离证明，不宣称严格恰好一次、全历史完整、所有原生 Dot 对话自动双向同步或秒级送达。

本 Skill 沿用随包保留的 [MIT 许可](LICENSE)。桥接运行依赖、官方客户端和协议参考遵循各自许可，安装时仍应保留源码包中的 NOTICE 与 licenses。
