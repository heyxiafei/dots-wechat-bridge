# 离线验证范围

这是 singletrial1 + steadyfuture1 的离线候选分发源码。依赖二进制、真实数据库、账号配置和私人报告不在包内。每次修改或导出后都重新验证实际目录，旧计数不充当新回执；新增未来来源协议须单独验收，不能用单条试验通过代替持续同步。

## 复现命令

在全新解压目录使用 Python 3.10+。先扫描还没有 `.venv` 的原始源码目录；再运行 `sh scripts/bootstrap.sh`，默认 `python3` 较旧时用 `PYTHON_BIN=/absolute/path/to/python3.11 sh scripts/bootstrap.sh`。脚本拒绝覆盖已有 `.venv`。成功后复核测试与 schema：

```sh
python3 -B scripts/scan-public-bundle.py . --json   # 创建 .venv 之前
sh scripts/bootstrap.sh                         # 或设置 PYTHON_BIN
.venv/bin/python -B -m unittest discover -s probe_tests
.venv/bin/python -B scripts/generate-schema.py --check
```

跨版本测试所需的 `rollback_baseline`、`localcutover_baseline` 与 `guarded_rollback` 均在 `probe_tests/fixtures/`；不再需要兄弟目录或 `WECHAT_COMPAT_BASELINE_DIR`。逐文件哈希及历史版本在 `probe_tests/fixtures/FIXTURE-PROVENANCE.json`，每组保留自己的 LICENSE 和 NOTICE。它们只是静态测试输入，**不可作为生产回退包**。测试仅在临时目录建合成数据库，子进程阻断网络；缺文件、哈希不符、测试失败或 skip 均应如实报告，不能称全套通过。

旧发行目录已有过测试回执，但本目录的结果应以**当前这一次**的完整命令输出、退出码和 skip 数为准。离线候选的跨进程回转仅证明合成状态下的兼容路径；不证明生产进程已切换或主 Dot 完成读回。扫描零发现还需逐文件人工复核。

Skill 安装器的预览、临时目录安装和拒绝覆盖由包内测试覆盖；本机全局安装需另行检查。Skill 的 frontmatter、相对链接与 MIT 文本应独立检查；若官方验证器未实际退出 0，不能将结构检查称作官方验证通过。

## 私人单条试验的脱敏证据边界

另一个私人运行实例曾用一条无附件短文完成限定试验：原文与最终回复在主 Dot 各有一次精确读回，手机截图可见测试原文与对应回复各一条、可见范围无重复。公开包只记录这项脱敏结论，不复制截图、账号、消息 ID 或聊天正文。该实例的真实回执既不是本分发目录新离线测试，也不替代任何安装者自己的端到端验收。

## 不在离线结果内

- 当前运行实例、独占锁身份和远端旧 writer 隔离。
- 主 Dot 实际取得本包 schema 中的工具契约和精确目标内容完整读回。
- 新用户环境的真实未来消息、Dot 写入、手机收件及端到端耗时；上述单条私人试验不可移用到另一实例。
- 当前产品原生 Dot 出站自动导出、严格恰好一次、全附件/全历史完整或 3 秒响应保证。

GitHub 提交、Skill 安装、插件连接、生产部署和真实闭环分别取得回执后再报告。人工审阅和扫描降低误带私人数据的风险，但不提供任意代码绝无敏感信息的保证。
