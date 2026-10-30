# 离线验证范围

这是公开 main 与 r7 候选合并后的公开源码离线验证范围，保留 singletrial1、steadyfuture1，并包括安装、额度协调者、附件授权、回复恢复及显式宿主配置。依赖二进制、真实数据库、账号配置和私人报告不在包内。每次修改或导出后都验证实际目录，原 r7 候选 464 项离线测试不是本次公开整合版的回执；新增未来来源协议须单独验收，不能用单条试验通过代替持续同步。

## 复现命令

在全新、无 `.git` 的归档目录使用 Python 3.10+。下面从公开仓库当前 `main` 创建归档目录；先记录实际提交 SHA，再核对文件并执行全套验证，不能沿用旧基线的回执。目录所在路径须符合 README 所列的 ASCII 无空格绝对路径约束。扫描器刻意拒绝 `.git`，不能直接对 Git 克隆目录执行 `scan-public-bundle.py .`。示例目录必须此前不存在：

```sh
git clone https://github.com/heyxiafei/dots-wechat-bridge.git dots-wechat-bridge-source
cd dots-wechat-bridge-source
git rev-parse HEAD  # 保存此完整 SHA，作为本次验证的源码版本
mkdir ../dots-wechat-bridge-reviewed
git archive --format=tar HEAD | tar -xf - -C ../dots-wechat-bridge-reviewed
cd ../dots-wechat-bridge-reviewed
python3 -B scripts/scan-public-bundle.py . --json
python3 - <<'PY'
from pathlib import Path
import hashlib, json
root = Path('.')
manifest = json.loads((root / 'PUBLIC-BUNDLE-MANIFEST.json').read_text())
expected = {'PUBLIC-BUNDLE-MANIFEST.json'}
for item in manifest['files']:
    path = item['path']
    data = (root / path).read_bytes()
    if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
        raise SystemExit('Manifest mismatch: ' + path)
    expected.add(path)
actual = {str(path.relative_to(root)) for path in root.rglob('*') if path.is_file()}
if actual != expected:
    raise SystemExit('Manifest file list mismatch')
print('Manifest verified:', len(expected), 'files')
PY
sh scripts/bootstrap.sh
.venv/bin/python -B -m unittest discover -s probe_tests
.venv/bin/python -B scripts/generate-schema.py --check
```

若系统默认 `python3` 较旧，用 `PYTHON_BIN=/absolute/path/to/python3.11 sh scripts/bootstrap.sh` 替代上面的 bootstrap 行。脚本拒绝覆盖已有 `.venv`；修改发行字节后，重新导出一个全新目录，再按新目录的退出码、失败数和 skip 数记证据。

跨版本测试所需的 `rollback_baseline`、`localcutover_baseline`、`guarded_rollback` 与 `p0_receipt_baseline` 均在 `probe_tests/fixtures/`；不需要兄弟目录或 `WECHAT_COMPAT_BASELINE_DIR`。逐文件哈希及历史版本在 `probe_tests/fixtures/FIXTURE-PROVENANCE.json`，原三组共 37 文件及各自 LICENSE/NOTICE 保持原字节。新增 p0 组六文件遵循根 LICENSE/NOTICE，其冻结快照来源可核，原始 Git commit 未核实并记为 `null`。四组共 43 文件只是静态测试输入，**不可作为生产回退包**。测试仅在临时目录建合成数据库，子进程阻断网络；缺文件、哈希不符、测试失败或 skip 均应如实报告，不能称全套通过。

发行边界定向检查可运行 `python -B -m unittest probe_tests.test_public_bundle probe_tests.test_fixture_integrity`。它验证固定清单完整性、缺必选模块时拒绝导出、历史夹具和 provenance 篡改拒绝、未知文件与敏感合成反例拒绝，并验证导出 Manifest 的实际大小与哈希。此命令不替代上述最终导出副本的全量测试和 schema 检查。AIHot 测试使用明显合成的宿主配置；真实运行必须由受信任宿主显式提供自己的授权配置，包内不含部署默认身份。新装普通 CLI 默认关闭 AIHot；合成配置测试通过只证明离线行为，旧已启用实例还须按 INSTALLATION.md 核实同一消费者接续，不能把三者混为迁移成功。

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
