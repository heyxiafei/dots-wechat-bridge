# 官方 App Server 额度读取适配器

`probe.app_server_usage_rpc` 从本项目旧候选迁回，为现有 `OfficialUsageProvider`
提供可选宿主 RPC 调用器。它不安装程序、不修改微信服务、订阅或凭据，不启动第二个微信处理者；
构造对象没有 I/O，也没有自动执行入口。生产宿主仍须明确注入它并沿用原
`OwnerCoordinator.handle_fixed_command` 的 owner、原 final 槽及发送权限检查。

可信宿主提供以下参数，不能从微信正文、附件或配置中的自我声明取得：

- 已获准运行、来源已核验的官方 `codex` 可执行文件绝对路径。
- `start_permit(executable)`：宿主在每次启动前核验对该官方进程及其认证生命周期的
  独立授权，必须返回 literal `True`；收到真实权限拒绝可抛 `OfficialReadDenied`。
  微信额度请求、已核账户绑定或配置中的布尔值不能代替这项授权。
- 独立核实的 `UsageBinding` 及证据引用。
- `account_matches(binding, account)`：与宿主现有账户证据核对的函数，必须
  返回 literal `True`。仅看到 ChatGPT 类型、套餐或用户自称身份不够。
- 若宿主已有经核验的官方账户 ID，传 `official_account_id`；届时额度响应
  缺 ID 或 ID 不同均停止。没有该字段时，不凭邮箱/套餐臆造 ID，仍须使用
  独立的账户对应证据。前后快照检查不是跨进程原子账户锁。

示意接线（不是可直接运行的配置或授权）：

```python
rpc = AppServerUsageRpc(
    lambda: StdioAppServerSession(
        trusted_codex_executable, start_permit=trusted_start_permit,
    ),
    binding=verified_binding,
    account_matches=trusted_account_matcher,
    official_account_id=verified_official_account_id_or_none,
)
provider = OfficialUsageProvider.from_existing_app_server(
    rpc, verified_binding, binding_evidence_ref,
)
# 或将 rpc 作为 from_host_configuration 的 authorized_app_server_rpc。
```

此处 `from_existing_app_server` 只负责使用注入的 RPC；该新调用器在每次实际
读取时开启一个短生命周期官方进程。不得把它解释为其他 Work 的工具已存在
于生产子进程。同步入口会等待该读取；异步宿主应使用自身受支持的工作线程
桥接，再向现有 async provider 注入可 await 的调用器，避免阻塞事件循环。
现有 `from_existing_app_server` 名称是能力注入接口，不是本适配器已复用既有进程的证明。
构造函数既不调用启动许可，也不启动进程；实际进入session时才核验许可。
没有默认可执行文件、默认账户匹配器、PATH发现、环境探测或自动事件接线。

每次按 JSONL 发送 `initialize`，等待成功后发送 `initialized`，然后使用
`account/read`（`refreshToken:false`）核验当前 ChatGPT 账户，读取一次
`account/rateLimits/read`，最后再检查账户快照。初始化期间的 `account/updated`
可能只是首次状态通知，由随后账户读取核验；开始额度读取后收到该通知、
可见账户改变、未知服务端请求或任何协议错误即丢弃结果；没有登录、thread、
turn、批准、自动重试或替代 provider。初始化参数固定，总共最多四次请求；
session是一次性的，关闭后不能再次启动。默认总读取预算 15 秒；结束时尝试清理
该调用拥有的进程组，包含两次最多 1 秒的退出等待，不注册后台自启。

`refreshToken:false` 仅表示不要求主动刷新。官方进程仍可能正常读取和更新
自身现有认证、进行网络请求及认证恢复；适配器不直接打开 auth/session
文件，不枚举环境变量，不索取或传递 token，不能承诺官方进程绝不刷新。

读取等待、协议请求大小与次数、行长、总输出和通知数量均有界。15秒是读取预算，
不包括宿主批准、操作系统进程创建或退出等待；不承诺完整调用有硬实时期限。
原 stdout/stderr、
账户信息和 RPC 错误 message/data 不写日志、不返回给 handler；仅返回
`rateLimits` / `rateLimitsByLimitId`，继续使用原格式器对 `codex` 桶、实际
窗口时长、读取新鲜度和缺失字段进行验证。数字只代表本次真实结果；余额
未接入经核验 mapper 时仍为未知。上游错误码不能单独证明权限拒绝，适配器
不据此自造分类；可信宿主已明确分类的 `OfficialReadDenied` 保持原停止语义。
退出清理失败时丢弃读取结果；已发生的真实拒绝不会被清理异常降级为普通失败。
这不证明进程已经退出，宿主仍须核实自己启动的进程生命周期。

测试用合成 stdio 帧和模拟账户验证，不启动真实 Codex，不查询真实额度。
这证明接口接线可行，不证明生产入口已注入、实际账户已对应或微信已收到额度。

本轮通过 OpenAI Docs 核对[官方 App Server 账户与额度协议](https://learn.chatgpt.com/docs/app-server#auth-endpoints)：
`account/read`支持`refreshToken:false`，`account/rateLimits/read`返回额度窗口；
managed ChatGPT认证由官方进程持有并自动刷新，因此“不强制刷新”不等于“不写认证”。
官方当前说明将现有App Server认证用于本地或开源应用，并明确不允许商业或托管服务使用该认证；
本模块作为可选本地宿主适配器，不把它作为托管服务认证方案。独立账户ID不是该页面对
`account/read`作出的保证，可选ID校验只用于宿主另有已核实际字段证据的情况。
安装版本是否支持各字段仍需宿主在获准后核验，合成测试不能替代真实接线验收。
