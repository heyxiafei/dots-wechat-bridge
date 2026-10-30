# 源码与 Skill 发布

发行范围是公开 `main` 提交 `48709d9fc72e9bdf4037c0ae907a75a376fd6ff5` 与 r7 离线候选源码的明确合并，加上公开发行所需的 AIHot 宿主配置注入及测试。保留 singletrial1、steadyfuture1 与历史兼容输入，加入安装、额度协调者、附件授权和回复恢复功能。该发行范围不表示已部署、已接入真实账户或已完成手机收件验收。[heyxiafei/dots-wechat-bridge](https://github.com/heyxiafei/dots-wechat-bridge) 是独立公开发布仓库；新的远端 commit、实际文件和本包 Manifest 须读回确认，不能沿用基线的测试回执。[RerrentLinden/dots-wechat-bridge](https://github.com/RerrentLinden/dots-wechat-bridge) 仅是 MIT 上游来源，不得将本候选推送到上游或其他仓库。

从克隆获取验证目录时，按 README 的 记录实际 `HEAD` + `git archive` 步骤生成无 `.git` 的目录。隐私扫描器拒绝克隆中的 `.git`；直接在克隆根目录运行扫描会得到 `private_directory`，这是预期拒绝，不表示归档目录不合格。安装前用 [离线验证说明](VALIDATION.md) 的命令逐项核对 Manifest 大小与 SHA-256，安装目录的真实绝对路径也须满足 ASCII 无空格约束。

## 可复现导出

使用现有 Python 3.10+ 环境，不复制依赖或历史 Git 目录：

```sh
python -B scripts/build-public-bundle.py --dry-run
python -B scripts/build-public-bundle.py --output ../reviewed-release
python -B scripts/scan-public-bundle.py ../reviewed-release --json
```

导出器使用逐项列出的固定文件清单，不扫描或打开 runtime、数据库、密钥、账号配置或日志。它拒绝已有输出，保留原候选；本版所列运行模块、配置示例、文档、测试、Skill 和安装脚本均为必选，缺文件即失败，不用目录通配符补齐。`PUBLIC-BUNDLE-MANIFEST.json` 记录合并依据、每个文件大小和 SHA-256，并在导出时重新生成，不能沿用旧包的 Manifest。

`probe_tests/fixtures/FIXTURE-PROVENANCE.json` 锁定四组共 43 个静态文件。原 `rollback_baseline`、`localcutover_baseline`、`guarded_rollback` 的 37 文件及各自 LICENSE/NOTICE 保持原字节；新增 `p0_receipt_baseline` 六文件与已复核的冻结快照一致，实际版本为 `0.5.0+local.video1.dotledger1.eventlease1`。其原始 Git commit 无法从已核证据确定，明确记为 `null`，不借用公开 main 的提交号。新增组遵循根 LICENSE/NOTICE，包内所有历史夹具只供测试，不能部署或作为生产回退包。

先在导出后的干净目录运行隐私扫描，再在**另一个拷贝**运行 `bootstrap.sh`、全量离线测试、schema 检查、Skill frontmatter 检查与临时目录安装验证。安装依赖产生 `.venv`，不应混入待上传目录。测试须无失败、无意外 skip；包外 sibling 目录和环境变量不能替代包内夹具。不能用 r7 候选原先的 464 项离线回执代替本次合并后导出包的验证。依赖由使用者另行安装，不捆绑 tunnel-client 或依赖二进制。将冻结候选与公开整合版逐文件比较，单列公开兼容修复、宿主配置注入和文档修改；不宣称运行模块仍与原候选完全相同。

## 隐私与许可证

排除个人路径、真实消息正文及 ID、账号/插件/隧道/订阅标识、回调签名、OTP、运行密钥、DB/WAL、日志、二维码、云端交接与本机完整报告。公开验收只使用合成样本和脱敏结果。扫描仅输出相对路径、行号与类别；仍需人工检查，扫描零命中不是绝对无敏感信息保证。

保留根 MIT LICENSE、NOTICE、Tencent 与 wx-bot 的许可证；三组原夹具各保留其许可和来源信息，新增六文件的根许可证关系在 provenance 中明示。wx-bot 与 Pocket Connector 仅作公开设计参考，不运行或捆绑其组件；参考版本、许可及实现差异见 NOTICE 和对应设计说明。OpenAI/tunnel-client 与 Python 依赖分别由官方发行版提供许可，不混入本包或重授权。增加或修改夹具时重新核对来源、完整文件哈希、允许清单和人工隐私审阅；不得扩大敏感值豁免来让扫描通过。

上传前检查目标仓库现有内容，避免覆盖不相关文件或历史；使用可审查的独立提交或草稿 PR。只有实际远端 commit/PR 回执与读回一致，才能称“已上传”。Skill 已安装、MCP 插件已连接、候选已部署及真实闭环通过是四个不同结果，需分别记录。
