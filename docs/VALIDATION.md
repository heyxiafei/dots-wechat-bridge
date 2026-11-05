# 离线验证与真实验收

验证以本次发布目录的实际字节为准。历史测试日志、候选源码、生产安装和手机收件是不同证据。

## 一条完整验证命令

先安装 `requirements.lock` 中的依赖，再导出固定清单；输出目录必须此前不存在：

```sh
python -B scripts/build-public-bundle.py --output ../reviewed-release
python -B ../reviewed-release/scripts/verify-release.py
```

验证器按顺序检查：

1. 隐私扫描，仅打开已列入清单的常规源码文件。
2. Manifest 类型、唯一安全路径、精确文件集合、大小与 SHA-256。
3. `scripts/generate-schema.py --check`。
4. `python -m unittest discover -s probe_tests` 等价的全量发现与执行；零测试、失败、错误或跳过均拒绝完成。
5. 测试后重新核对发布目录，确保测试没有改变待发布字节。

输出是逐阶段 JSON 和 unittest 摘要。退出码 0 才代表全部所选检查通过。`--structure-only` 不运行 schema 和测试，只证明静态结构与隐私检查通过。

源码克隆目录内含 `.git`；扫描器会拒绝。使用导出目录验证，或先记录 `git rev-parse HEAD` 再 `git archive` 到全新目录。虚拟环境放在发布目录外，用它的 Python 验证发布目录。待发布包不包含 `.venv` 或依赖二进制。

## 回归范围

包括 owner-only、来源完整性、引用、附件、ACK/final槽、unknown不重发、事件订阅与续期、回调边界、协调者和官方额度适配、安装预检、回退兼容，以及 AIHot 来源生产者与两个渠道的通知消费者。AIHot 生产者历史测试由 `probe_tests/test_aihot_producer.py` 纳入统一入口，使用合成数据与临时目录，并阻断未模拟的网络。

`probe_tests/fixtures/FIXTURE-PROVENANCE.json` 固定四组共43个历史文件。原文件、许可证和来源说明保持不变；它们只供离线测试，不能部署或作为生产回退包。缺失或哈希不同必须报错，不能删除旧测试来通过重构。

发布工具的定向检查：

```sh
python -B -m unittest probe_tests.test_public_bundle probe_tests.test_fixture_integrity probe_tests.test_verify_release
```

定向检查不替代全量发布验证。GitHub CI 配置运行 Linux/macOS 和 Python 3.11/3.13；实际执行结果以该提交的 GitHub 回执为准，本机结果不能冒称整个矩阵已通过。

## 真实验收另行记录

- 精确版本是否安装、旧writer是否退出、独占锁及原私有数据接续。
- 原MCP工具、有效订阅、账号绑定及当前官方额度reader。
- 全新本人文字、菜单、额度、每类附件的实际处理和手机收件。
- AIHot自然未来事件的来源判断、原账本唯一提交、微信及邮件各自收件。
- 进程恢复、整机重启、断网与unknown对账。

API `accepted`、队列 `committed`、健康检查、测试通过都不证明手机已收到。微信发送时间与来源公告时间分别记录，不推算不存在的端到端延迟证据。扫描降低误带敏感内容风险，仍需人工检查公开内容与许可证。
