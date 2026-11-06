# 参与开发

在隔离源码副本中修改，不在正在收发的安装目录开发。测试只使用合成账号、消息和临时数据库，不连接真实微信、不装服务，也不申请新的订阅。

安装 `requirements.lock` 中的依赖后运行：

```sh
python -B -m unittest discover -s probe_tests
python -B scripts/generate-schema.py --check
python -B scripts/build-public-bundle.py --output ../reviewed-release
python -B ../reviewed-release/scripts/verify-release.py
```

最后一条命令检查文件清单、哈希、隐私、工具协议和全套测试；任何失败或跳过都会使验证失败。`--structure-only` 仅检查发行结构，不代表测试通过。导出目录必须不存在；导出器不会覆盖原文件。

新增源码、测试或文档时，逐个加入 `scripts/scan-public-bundle.py` 的固定清单。不要添加目录通配符，不要放宽敏感值检查。历史兼容夹具、来源清单和许可证必须保留。Manifest 由导出器生成，不能用旧清单代表新字节。

PR 说明写清用户可见行为、测试证据、兼容影响和未验收项。API accepted、离线测试与手机收件是不同证据。涉及来源判断、发送重试、账号映射和生产接续的修改，需要额外边界测试。生产数据、凭据、完整日志和私人交接材料不进入 Git；提交前使用干净导出目录审查。

CI 在 Linux/macOS 和两个 Python 版本上执行离线验证，不部署、不使用账号密钥、不发送微信。实际 CI 运行结果以 GitHub 的检查回执为准。
