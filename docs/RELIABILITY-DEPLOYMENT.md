# singletrial1 部署与兼容回退

本文件描述部署或恢复时的核验步骤，不代表任何主机的当前状态、主 Dot 内容读回或手机送达已验收。先读 [单来源契约](SINGLE-SOURCE-TRIAL.md)。保留已有授权范围，不反复索取相同许可；新凭据、权限、持久访问与真实外发需分别明确。

## A：关闭状态部署

1. 使用运行中服务的正式只读工具记录版本、健康、唯一订阅、绑定、准入、回执及队列摘要。不得将候选 `probe.py --status` 指向生产来做“只读”检查：初始化会迁移数据库。
2. 确认准确的托管服务、supervisor/client/worker 关系及独占锁。文件被打开不等于持有独占锁；查询失败不证明服务已停止。无法确认就报告所缺证据，不盲停，也不绕过查询拒绝。
3. 对已审 localcutover1 基线，拟替换 `probe/probe.py`、`probe/weixin.py`、`probe/dotjournal.py`、`probe/dotcutover.py`，新增 `probe/dotcompleteness.py`，更新 `docs/mcp-schema.json`。部署前逐文件核对实际基线与发行 manifest；其他版本必须重新生成差异。安装 Skill 本身不需要这些生产动作。
4. 在获准维护窗口内短停同一服务、确认退出与锁释放，保存私有 SQLite 一致快照和源码清单，安装冻结文件并以同一实例执行增量迁移、重启。快照只能用于取证或另行评估的灾难恢复，不能覆盖后来产生的新数据。短停可能延迟入站、事件和回复；无法保证零中断。
5. 保留唯一持续订阅、账号绑定、凭据、回调允许列表、资源限额和 ACK 策略。不新建消费者，不卸载/停用订阅，不覆盖其他任务的候选。若共享服务有并行变更，先协调并确认当前基线。
6. 重启后核对健康、单实例、数据保留、原 MCP 只读调用。批量和单来源准入都保持关闭：必须检查 `any_new_admission_enabled=false`，不能只读 `state=closed`。
7. 在原插件更新工具目录，要求目标主 Dot 看到与本包 `docs/mcp-schema.json` 一致的完整工具清单，并成功调用新的只读入口。其他子任务可调用不代替主端验收；工具数量以实际 schema 和主端发现结果核对，不引用历史计数。

本阶段只能报告关闭状态部署。未经确认的现场身份、旧 writer 风险、主端工具或内容读回不能靠部署成功推定。

## B：另行允许的一条未来本人消息

真实 Dot 写入前，核对同一主 Dot 的真实 scope；用已有无敏感目标证明精确目标 ID 的内容与元数据可完整读回（`partial=false` 或平台等价完整性证明）。无关历史窗口不完整不能阻塞新来源首次许可，但空搜索或 partial 也不能证明 unknown 未发送。

先确定采用哪一种验收口径。严格唯一 writer 需要可验证的旧远端执行者终止或目标侧写入隔离及在途排空；本地 guard、webhook `delivered` 和任务提示词均不能证明这一点。若平台无法提供该证据，只有在本人**另行明确接受旧直写可能额外或重复写入且次数未知的风险**后，才可用现有 `bounded_local_ownership` 做一条未来合成来源试验：获准把原入站任务的旧直接 Dot 镜像分支改只读并读回配置，保留同一持续订阅和普通微信 final，不新增写入者。此时仍报告 `remote_legacy_writers_fenced=false`、`strict_exactly_once=false`。未取得批准则不试写。

批准范围应写明：真实且将来使用的固定 Dot target scope（arm 只能一次且永久绑定，不能用临时房间）、登记一次性短文试验、选择本人登记后新发且哈希匹配的单个 ID、**新 journal 路径**最多两次 Dot 写入（原文和实际 final 各一次）及原唯一回复者的一条微信 final；旧远端直写不受这一上限约束。先 arm 成功，再由本人发送唯一短文；1–256 UTF-8 字节，无附件。只允许该来源，不批量 enable，不选旧来源。

每部分 prepare → 一次 authorize → 一次写入 → 原目标 ID commit → 精确内容读回。授权回包丢失、发送后回包/commit 丢失均保留 unknown 或永久授权事实，不换 attempt 重新许可，不补发。未通过内容读回不称 verified，手机收到要由本人确认。一次成功不等于附件全覆盖、长期稳定或 3 秒响应。

结束时使用稳定 request_id 正式 close，读回 `any_new_admission_enabled=false` 与 `selection_active=false`，再逐个按原 journal/attempt 对账未决许可；close 不撤销已发 permit，unknown 继续保留，不宣称后续远端写入已不可能。重启不会替代 close；一旦 arm，试验限制持久存在，不能通过删表或回退取消，也不能直接改为长期批量准入。本候选另含[持续未来来源协议](STEADY-FUTURE-ADMISSION.md)，它是新增的独立准入与迁移关卡；一条试验通过不会自动启用它，更不等于项目持续同步已上线。

## C：另行审议的持续未来来源候选

本节不授权生产动作。部署前必须对当前**准确原实例**重新核对文件/数据库基线、完整兼容回退方案、主 Dot 工具目录与同 scope 内容读回、原自动化旧直写分支限制、本人对持续准入及旧远端重复风险的批准。若现场生产仍为 singletrial1，不能把此源码目录、离线迁移或新 schema 当作已部署；实际生产版本必须从已核实实例读回。预计运行差异涉及 `probe/probe.py`、`probe/dotcutover.py`、`probe/dotjournal.py` 与 `docs/mcp-schema.json`；实际文件清单以现场字节差异为准，不整包覆盖。

新候选只在已选 trial 正式 close、原文和实际 final 的 caller-reported 正向读回齐备且无该来源已授权/unknown 时允许另行启用未来代际。每代限制来源数，暂停/close 后新许可停止，旧 permit/unknown 仍须按原 ID 对账；恢复新建代际，间隙消息不补。保留单条 trial、旧来源 guard、媒体逐件状态及全部数据库增量，不恢复旧库或调用旧 bulk enable。真实部署和每次准入须另行限定批准，远端 fence 仍为 false。

## 回退边界

只允许已冻结、经过同数据库离线实测的兼容回退包。必须保留新版 `weixin.py`、`dotjournal.py`、**singletrial1** `dotcutover.py` 三个模块及全部当前数据库增量、guard、trial、attempt/generation、许可、unknown、回执和新入站 manifest。

先正式 close 并读回所有准入关闭，再短停同一服务、安装兼容包、重启并核对数据和关闭状态。只撤回完整性聚合等外围整合；三模块自身若有故障必须在保留安全状态的基础上修复前进。不得临场拼接两模块旧配方，不得整套回退 localcutover1，不得用旧数据库覆盖现有状态。回退不能撤销已发生的远端发送。

兼容包与其哈希、实际测试回执必须单独交付。源码包中的 `probe_tests/fixtures/rollback_baseline`、`localcutover_baseline`、`guarded_rollback` 均只是旧代码测试夹具，**不能部署**；它们的存在不等于生产兼容回退包已装好。
