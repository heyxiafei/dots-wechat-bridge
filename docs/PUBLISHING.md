# 源码与 Skill 发布

发行范围是 singletrial1 + steadyfuture1 公开候选源码、包内兼容测试夹具与安装指南。[heyxiafei/dots-wechat-bridge](https://github.com/heyxiafei/dots-wechat-bridge) 是本人的独立公开发布仓库；仓库已存在不代表本候选已上传。发布前核对目标分支、最新 commit 和差异，发布后以该仓库的实际 commit/tag 与本包 Manifest 读回确认。[RerrentLinden/dots-wechat-bridge](https://github.com/RerrentLinden/dots-wechat-bridge) 仅是 MIT 上游来源，不得将本候选推送到上游或 `dot-blender-studio`。

## 可复现导出

使用现有 Python 3.10+ 环境，不复制依赖或历史 Git 目录：

```sh
python -B scripts/build-public-bundle.py --dry-run
python -B scripts/build-public-bundle.py --output ../reviewed-release
python -B scripts/scan-public-bundle.py ../reviewed-release --json
```

导出器使用固定文件清单，不扫描或打开 runtime、数据库、密钥、账号配置或日志。它拒绝已有输出，保留原候选。`requirements.lock`、单来源试验契约与测试、Skill、安装脚本和三个历史兼容夹具均须包含在发行范围中。`PUBLIC-BUNDLE-MANIFEST.json` 记录每个文件大小和 SHA-256；`probe_tests/fixtures/FIXTURE-PROVENANCE.json` 记录三组静态历史源码的版本和逐文件哈希。包内夹具只供测试，不能部署。

先在导出后的干净目录运行隐私扫描，再在**另一个拷贝**运行 `bootstrap.sh`、全量离线测试、schema 检查、Skill frontmatter 检查与临时目录安装验证。安装依赖产生 `.venv`，不应混入待上传目录。测试须无失败、无意外 skip；包外 sibling 目录和环境变量不能替代包内夹具。不能用开发目录测试结果代替实际导出包。依赖由使用者另行安装，不捆绑 tunnel-client 或依赖二进制。生产运行模块与冻结候选逐字节核对，文档/发布脚本改动另列。

## 隐私与许可证

排除个人路径、真实消息正文及 ID、账号/插件/隧道/订阅标识、回调签名、OTP、运行密钥、DB/WAL、日志、二维码、云端交接与本机完整报告。公开验收只使用合成样本和脱敏结果。扫描仅输出相对路径、行号与类别；仍需人工检查，扫描零命中不是绝对无敏感信息保证。

保留根 MIT LICENSE、NOTICE、Tencent 与 wx-bot 的许可证；三组旧代码夹具各保留其许可和来源信息。wx-bot 仅作参考，不 fork 或运行其组件。OpenAI/tunnel-client 与 Python 依赖分别由官方发行版提供许可，不混入本包或重授权。增加或修改夹具时重新核对来源、完整文件哈希、允许清单和人工隐私审阅。

上传前检查目标仓库现有内容，避免覆盖不相关文件或历史；使用可审查的独立提交或草稿 PR。只有实际远端 commit/PR 回执与读回一致，才能称“已上传”。Skill 已安装、MCP 插件已连接、候选已部署及真实闭环通过是四个不同结果，需分别记录。
