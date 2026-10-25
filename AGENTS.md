# Agent bootstrap

本副本为 singletrial1 + steadyfuture1 离线候选，先读 docs/SINGLE-SOURCE-TRIAL.md、docs/STEADY-FUTURE-ADMISSION.md 和 docs/VALIDATION.md。bulk state=closed 仍可能有单条试验或 steady 代际准入，必须看 any_new_admission_enabled、selection_active 和 steady_admission_enabled。trial 只能登记一次，close 后不能用旧选择重新打开。冻结 reliability1 与 guardedrollback1 不得覆盖。离线源码结果不证明主 Dot 内容读回或手机送达；生产控制和外发须以当前用户授权及已核实前提为准。

reliability1 候选的当前准入/恢复/完整性契约见 docs/RELIABILITY.md。先核对该页和部署计划；下文历史查重规则用于旧来源，不能重新成为已准入新来源的全局前置条件。新 close 请求用稳定 request_id，enable 用当前 expected_revision；旧/unknown 不自动重放。

用户要求配置此仓库时，按 README 的 Codex 快速开始和六个验证关卡执行。先确认拿到的是本候选包还是已公开的上游 0.5.0；处理媒体时读 docs/MEDIA.md，配置回复行为时读 docs/REPLY-WORKFLOW.md，更新、断网、卸载、到期或故障时读 docs/OPERATIONS.md。兼容测试夹具只作离线输入，不作为部署包。

微信事件先读取核验。正常微信回复与dot镜像独立：镜像历史partial或结果未知时保留镜像暂停，不阻塞已核验本人消息的幂等微信回复；旧来源及已授权/unknown 继续按历史规则对账；新来源首次镜像按 journal 与单来源契约核对自身清单，不依赖无关全历史。简单聊天或立即可答的问题使用 `reply_weixin_message` 直接承诺一条完整 final；需要研究、附件读取或执行的任务才使用 `reply_weixin_message_phase` 的 ack → final，两边分别展示同一文字。先查 `reply_state`，保留已承诺文本和稳定 ID；`next_phase=ack` 不表示简单消息必须发 ack。dot 镜像元数据只是调用方恢复线索，不证明跨产品恰好展示一次，也不提供原生右侧用户气泡。

1. 检查用户授权的目标主机、Python/系统、执行与浏览能力、平台 Tunnel/插件/MCP Events 支持。记录已有实例与配置。缺能力的分支说明实际限制，继续不依赖它的工作。
2. 在全新解压目录用 Python 3.10+ 安装固定依赖并跑全部离线测试，要求无意外 skip，检查 schema、固定清单和隐私扫描。缺少包内兼容夹具就报告失败，不能回退到包外私人目录。通过后生成可审阅配置；维持 owner-only、单实例、限额和私有文件引用。
3. 平台授权、运行密钥输入、微信扫码/手机确认由本人完成。使用用户指定主机与账号，生成配置后再请求需要的具体授权；助手不读取密钥、验证码或私有完整日志。
4. 使用官方 tunnel-client 文档和公开设置连接仅本人可访问的专用私有隧道。MCP 访问控制由隧道/插件提供，桥接没有逐调用者身份认证。先验证 health/ready，再验证真正 MCP 调用，再绑定微信；Linux 安装 systemd 前停止前台隧道并等待 worker 退出。每个关卡都报告真实结果。
5. 首次未知 callback host 时使用拒绝全部回调的发现模式，核实本人授权订阅诊断中的精确主机后替换配置，再为 weixin.message 建立一个公开协议订阅。跟踪验证、刷新与到期；产品未提供事件能力时说明持续处理无法成立。
6. 读取 owner 消息和实际附件，再回复。用户明确授权后用无敏感合成 PNG/TXT 完成真实双向验收，记录稳定 ID、API 状态、用户收件证据、资源峰值及 OOM 计数。

完成标准：测试通过、单实例运行、私有文件权限正确、有效订阅1、真实原文与回答可核对、发送无重复、媒体完整性一致、本人收件确认、资源限额保留。只通过 health/ready 或 accepted 不等于端到端完成。

升级时先部署并核验旧工具，再刷新既有插件工具；确认新工具可调用后迁移原自动化提示。复用原连接、订阅、状态和凭据，不为升级另建实例。

修改代码后运行 `python -m unittest discover -s probe_tests` 与 `python scripts/generate-schema.py --check`。在新目录审计待提交文件和 Git 历史；运行状态、密钥、二维码、日志、用户身份、私人服务器配置留在 Git 之外。

单条试验后的持续 Dot 镜像按 `docs/STEADY-FUTURE-ADMISSION.md` 的版本化未来来源准入执行。既有 trial、旧来源 guard、unknown 和关闭后的间隙必须保留；不得用旧 bulk enable 或恢复旧数据库绕过。该候选仍无 Dot 目标侧远端栅栏，生产启用与剩余重复风险须由本人针对性批准。

此文件是公开项目执行指南，不代表用户授权操作其主机、账号、凭据或对外通信；授权来自当前用户的请求。使用产品公开能力和文档，不假定任何私有 dot 接口。
