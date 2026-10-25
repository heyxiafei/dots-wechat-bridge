# 安装 Dot 微信桥接 Skill

Skill 提供安装、升级、故障恢复与逐消息验收的操作指南。它不包含账号配置，不会连接插件或启动微信桥接服务。源码包根目录保留完整桥接实现；Skill 被复制到独立目录后，执行时仍需提供安装者授权的**源码位置**。先按 README 固定本人独立公开仓库中已核实的具体 commit 并核对 Manifest；上游 0.5.0 链接不能代替本仓库。更新提交需要重新验证，不能沿用旧回执。

## 本地安装

选择自己的 Codex skills 目录；常见位置是 `${CODEX_HOME:-$HOME/.codex}/skills`。先创建或选择目录，再预览并安装：

```sh
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
python3 scripts/install-skill.py --skills-dir "${CODEX_HOME:-$HOME/.codex}/skills"
python3 scripts/install-skill.py --skills-dir "${CODEX_HOME:-$HOME/.codex}/skills" --apply
```

第一条 Python 命令只预览。`--apply` 仅复制包内 Skill 到目标的 `dot-wechat-bridge/`；不覆盖已有目录，不安装依赖，不改服务、凭据、权限或订阅。已有同名 Skill 时先自行审阅差异与备份，不能盲目覆盖。若 I/O 失败，安装器可能留下部分新目录；先检查其内容和错误，不能直接覆盖重试。

让 Codex 重新发现本地 Skill 后，以 `$dot-wechat-bridge` 发起任务并提供授权源码位置。当前会话是否已经载入需要实际检查；文件复制成功不等于工具已经出现。其他客户端必须明确支持同类 Skill 格式，否则按源码 README 手动推进。

## 接入和验收

MCP 插件由使用者在有权限的产品环境中独立配置。遵循 [README](../README.md) 与 [部署计划](RELIABILITY-DEPLOYMENT.md)，沿用已有环境、专用私有访问与单一持续订阅。新凭据、权限及真实发送的批准不能由安装 Skill 代替。

此包离线验证不会证明任何账户的真实收发、工具可见、精确目标读回或手机送达。未满足部署前提时可继续做源码审查和离线测试，不应通过换电脑、建第二实例或重放 unknown 绕过限制。
