# 可选文本模型 API 适配

`foundation/providers.py` 是独立的纯文本 API 边界，不是 DoT、GPT Work 或它们的工具执行环境。五家适配器均有请求构造、响应解析、用量投影和错误分类；目前只有注入假传输的离线验证，**没有真实模型调用、凭据核验或微信收件验收**。

构造和 `health()` 不读取凭据、不联网。默认 `allow_network=False`，直接提交返回 `network_not_authorized`。真实部署须先核本人授权的供应商、项目、模型和费用范围，再由可信宿主显式启用。注入 transport 是受信开发接口，不能由聊天或插件数据构造。

## 协议与接口

```python
from foundation.providers import ApiProvider, ProviderConfig

config = ProviderConfig(
    backend_id="openai-text",
    provider="openai",
    model="REPLACE_WITH_AUTHORIZED_MODEL_ID",
    credential_env="OPENAI_API_KEY",
    max_output_tokens=1024,
    max_input_bytes=65536,
    timeout=20.0,
)
backend = ApiProvider(config)  # 仍禁止网络；本段没有读取 key
assert backend.health()["state"] == "blocked"
```

提交 `BackendRequest`，返回同一 `request_id` 的 `BackendResult`。测试可传 `transport(url, headers, payload, timeout)`，返回 `TransportResponse(status_code, body)`，并使用假的 `credential_resolver(env_name)`。生产默认传输为标准库 urllib，无新增 SDK 依赖。只有获准提交时才读取对应环境变量；没有认证文件搜索、登录、钥匙串或其它后端凭据回退。

| provider | 固定文本接口 | 输出预算参数 | 凭据引用 | 独立处理 |
|---|---|---|---|---|
| openai | `https://api.openai.com/v1/responses` | `max_output_tokens` | `OPENAI_API_KEY` | Responses input/output、`store=false`、完整状态与final phase、input/output/total用量 |
| anthropic | `https://api.anthropic.com/v1/messages` | `max_tokens` | `ANTHROPIC_API_KEY` | Messages文本块、版本及workspace header、stop_reason、缓存用量分项 |
| kimi | `https://api.moonshot.cn/v1/chat/completions` | `max_completion_tokens` | `MOONSHOT_API_KEY` | 当前输出参数、缓存读取/写入分项、独立余额错误码 |
| deepseek | `https://api.deepseek.com/chat/completions` | `max_tokens` | `DEEPSEEK_API_KEY` | Chat完成原因、prompt缓存命中/未命中分项 |
| ark | `https://ark.cn-beijing.volces.com/api/v3/chat/completions` | `max_completion_tokens` | `ARK_API_KEY` | 普通API端点、总生成预算、思考用量分项及服务端model fallback检测 |

模型或接入点 ID 必须配置，不内置“当前推荐模型”，也不查询模型列表。单次任务的后端、模型和配置版本由桥接核心固定。这里支持把 `request.model` 作为受信路由快照；模型正文和附件无权修改它。参数上限是本地保护范围，不保证任意模型都接受该值；具体模型的上下文、地区、权限和额度仍需真实联调。

五家 API 彼此独立。凭据环境变量按供应商命名空间校验，允许如 `OPENAI_DEV_KEY` 的同供应商独立引用，拒绝把 `OPENAI_API_KEY` 直接配给 Kimi。模型输入不包含 API key、WeChat context_token、平台线程 ID 或原始错误对象。

## 能力声明和历史

本轮能力仅为文本请求和已返回 token 用量。图片、文件、音频、视频、流式、工具调用、审批、取消、远端任务查询及主动回调均明确为不支持；有附件时在读取 key 和网络调用之前返回 `media_not_supported`。API 本身支持某功能，不表示这个适配器已经实现。

`history` 只允许桥接核心已批准的 user/assistant 纯文本历史；最多64条，并与本条文本共用输入字节预算。只携带本地已经完成的聊天final，不导入 DoT/Work 的中间更新、工具状态或私有记忆。OpenAI 历史 assistant 被标为 final_answer；响应中的 commentary 和 reasoning 不作为最终微信回答，函数/工具结果不会执行。

后端自己的 `backend_session` 仅原样关联本任务，不发送到其它供应商，也不调用供应商原生会话存储。跨供应商迁移历史、摘要或附件需要桥接策略另行授权。这个模块不自行恢复历史、生成摘要或创建持久记忆。

## 超时、失败与费用

每个请求只调用一次 transport，没有自动重试或跨厂商 fallback。HTTP 401、403、404、429、5xx、余额不足、重定向与输出截断有固定错误分类。HTTP 请求或返回不确定时保持 `unknown`；异常原文不进入结果。失败和过期结果不带可发送正文。

传输只允许表中固定 HTTPS 完整端点，拒绝重定向；请求和响应分别限1 MiB，有限 socket timeout，读取分块时再检查总预算。urllib/DNS及注入传输不提供可强制取消的整个调用硬时限，因此不能声称请求一定在20秒内返回或上游已经停止。原任务截止在调用前再次检查，返回晚于截止则隐藏正文并标记 `late_result_not_deliverable`；不重试、不宣称已取消远端请求。

默认 urllib 沿用进程已有网络代理设置，不修改系统代理。这里没有为不同接口配置新的代理或自动修复网络。后端网络故障不应被归类为微信掉线。

用量只保留响应中的非负整数白名单，缺失值保持缺失。Anthropic 缓存输入与普通输入分项保存；其total由已返回分项计算，其他供应商不重复累加缓存子项。无单价估算、自动充值、余额读取或reset操作；结果里的 token 数不是最终账单。失败或超时后未拿到用量时不能按0计费。

输入字节和输出 token 预算是单次请求限制。它们不等于每日/月度货币预算，也不证明不会产生结算偏差；每日/月度累计控制需由桥接任务账本及获准计价配置另行实施。ChatGPT Pro、GPT Work、Kimi会员、厂商Coding Plan与普通API余额不是同一权益。

## 供应商范围及官方依据

下列资料于2026-11-06读取，仅使用官方文档。没有访问任何模型计费端点。

- OpenAI：采用 [Responses 创建接口](https://developers.openai.com/api/reference/resources/responses/methods/create) 的input/output与输出预算；[会话状态](https://developers.openai.com/api/docs/guides/conversation-state)说明便携文本历史及assistant phase。这里不实现完整的工具/推理状态往返。
- Anthropic：[API概览](https://platform.claude.com/docs/en/api/overview)和[认证](https://platform.claude.com/docs/en/manage-claude/authentication)规定Messages、认证与workspace选择；[2026-10-21迁移说明](https://platform.claude.com/docs/en/api/migration-2026-10-21)说明当前版本和header变化。本适配默认该版，使用Bearer，不发送已移除的采样参数。若key没有绑定单一workspace，须配置 `anthropic_workspace_id`；本轮没有核验任何workspace。
- Kimi：[Chat API](https://platform.moonshot.cn/docs/api/chat)、[故障排查](https://platform.moonshot.cn/docs/guide/faq)与[缓存说明](https://platform.moonshot.cn/docs/guide/use-context-caching-feature-of-kimi-api)。本轮固定中国站开放平台端点，不兼容导入Kimi Code或会员凭据，不声称国际站凭据通用。
- DeepSeek：[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)规定请求、完成原因及缓存用量。仅使用最终content，不发送或保存reasoning_content。
- 火山方舟：[Chat API](https://docs.volcengine.com/docs/ark/chat-api?lang=en)规定普通API端点、参数与用量；[Coding Plan接入说明](https://docs.volcengine.com/docs/82379/2277824)显示专属端点和key不同。当前代码仅实现普通按量API，没有自动使用Coding Plan或替用户开通服务；配置前须确认这条费用路径。

## 验证状态

| 项目 | 当前状态 |
|---|---|
| 五家请求/响应/用量/错误映射 | 实现，合成测试通过 |
| 固定HTTPS端点、重定向拒绝、字节与socket预算 | 实现，假HTTP对象测试通过 |
| 真实凭据和模型权限 | 未读取、未核验 |
| 真实API请求与费用 | 未调用 |
| 两家真实后端切换 | 未验收 |
| 手机收件 | 未验收 |

局部测试命令：`python -B -m unittest foundation_tests.test_foundation_providers -v`。测试注入假的key解析、HTTP与时钟，不启动官方程序、微信消费者或网络。需要真实联调时另以获准的新任务进行；不能重放以前已处理的微信请求。
