# dot × 微信桥接

把本人的微信私聊接到支持 MCP Events 的助手，持续处理文字、图片和文件；可选连接 AIHOT 的 Codex 重置监控。桥接负责身份、队列、附件和状态，助手负责理解请求与执行任务。

**0.6.0+refactor1 是隔离重构候选。** 源码与历史兼容测试已迁入同一项目，生产部署和手机收件需要各自验收。已有实例曾完成文字双向收件；当前 `/help` 的真实入站字段故障、额度宿主接线及 AIHot 新机接续仍需完成，详见[功能与迁移状态](docs/MIGRATION.md)。

## 使用方式

- 在已绑定的微信私聊直接提出需求；简单问题收到一条完整回答，复杂任务可先收到进度，再收到结果。
- 发图片或文件时附上希望完成的事；文件上限 20 MiB，语音使用微信上游已有的文字转写。
- `/help` 显示菜单；“额度”“查额度”“查一下额度”等已列明入口请求当前 Codex 周额度。官方读取器或本人账户映射缺失时显示未知。
- 普通 dot 对话仅在本人明确要求时发送微信。当前部署的旧 dot 镜像保持只读。
- AIHot 的微信和邮件通知分别配置、分别验收；整合源码不会自动启用通知。

`queued` 表示已排队，`accepted` 表示微信接口接受；手机实际收到由本人确认。发送结果不确定时查询原记录，避免重新发送。

## 开发与验证

要求 Python 3.10+；macOS 或 Linux。使用新源码目录，不在运行目录中开发：

```sh
git clone https://github.com/heyxiafei/dots-wechat-bridge.git
cd dots-wechat-bridge
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -B scripts/build-public-bundle.py --output ../reviewed-release
.venv/bin/python -B ../reviewed-release/scripts/verify-release.py
```

导出器只复制固定清单中的源码，不复制 Git 历史、账号配置、数据库或密钥。验证命令检查精确文件集合、哈希、隐私、工具协议和全套离线测试。输出目录需不存在，实际绝对安装路径只允许 ASCII 字母、数字、`_`、`.`、`/`、`-`。

[开发说明](CONTRIBUTING.md) · [验证范围](docs/VALIDATION.md) · [发布流程](docs/PUBLISHING.md)

## 安装与原实例接续

让助手先读取本仓库 `AGENTS.md` 与[安装说明](docs/INSTALLATION.md)，核对目标主机、原服务、私有 MCP Tunnel、插件、唯一事件订阅和账号绑定。已有实例复用当前数据与连接，先完成离线验收，再审阅具体接续与回退步骤。生产数据留在原私有运行目录。

安装、平台授权、微信绑定、事件订阅、API 接受、手机收件是独立关卡。运行密钥由本人在私有终端输入，扫码与手机确认由本人完成。MCP 侧访问控制由私有 Tunnel 和插件提供；仅向本人授权的调用方开放。

[安装与预检](docs/INSTALLATION.md) · [运维恢复](docs/OPERATIONS.md) · [迁移与验收](docs/MIGRATION.md)

## 项目结构

|目录/模块|职责|
|---|---|
|`probe/weixin.py`|兼容原 Store API、账号及持久状态组装|
|`probe/weixin_inbound.py`、`weixin_dispatch.py`、`weixin_protocol.py`|入站捕获、唯一发送者、微信 HTTP 边界|
|`probe/command_*`、`fixed_commands.py`|命令目录、上下文校验、菜单与额度展示|
|`probe/owner_coordinator.py`、`official_usage_provider.py`|可信宿主协调、官方额度能力注入|
|`probe/app_server_usage_rpc.py`|可选本地官方额度读取器，显式授权后启动|
|`probe/rpc_*`、`callback_transport.py`、`service_runtime.py`|MCP 协议、分发、诊断、回调与进程生命周期|
|`integrations/aihot/`|AIHOT 来源监控和共享通知生产者|
|`probe/aihot_notice_consumer/`、`aihot_email.py`|独立微信/邮件消费、未来切点与持久去重|
|`probe_tests/`|合成测试及冻结的历史兼容夹具|

[重构设计](docs/ARCHITECTURE.md) · [用户操作说明](docs/USER-GUIDE.md) · [AIHot 接入](docs/AIHOT-INTEGRATION.md)

本地官方额度读取器的账户核验、启动条件与限制见 [App Server 额度适配](docs/APP-SERVER-USAGE.md)。安装模块本身不会启动该会话，也不代表微信协调宿主已接通它。

## 来源与许可

桥接基于 [RerrentLinden/dots-wechat-bridge](https://github.com/RerrentLinden/dots-wechat-bridge)，保留 MIT 许可证与历史兼容夹具。

AIHOT 原项目是 [KKKKhazix/AIHOT](https://github.com/KKKKhazix/AIHOT)，官方署名为**数字生命卡兹克**。其开源代码采用 MIT；项目名称与 Logo 的范围另见原项目说明。此处迁入的是本地监控与桥接集成，不冒称重新分发了 AIHOT 线上服务。原项目、作者、引用版本及许可边界见[AIHOT 来源说明](docs/AIHOT-PROVENANCE.md)。

[LICENSE](LICENSE) · [NOTICE](NOTICE.md) · [第三方许可证](licenses/)

本项目独立于 OpenAI、腾讯及 AIHOT 原项目，未获其背书。生产状态、密钥和私人聊天留在 Git 之外。
