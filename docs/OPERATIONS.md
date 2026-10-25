# 更新、恢复与到期

## 每次检查

在最终安装目录操作。Linux 安装后的 runtime 属于 dotsbridge，目录权限700，不向普通登录用户开放。以服务用户运行只读状态脚本：

```sh
cd /opt/dots-wechat-bridge
sudo -u dotsbridge .venv/bin/python scripts/safe-status.py --state-dir runtime/state
systemctl show dots-wechat-bridge.service --property=ActiveState,SubState,UnitFileState,MemoryCurrent,MemoryMax,CPUQuotaPerSecUSec
```

macOS 前台部署使用 `.venv/bin/python scripts/safe-status.py --state-dir runtime/state`，无须服务用户或 systemd。工具 get_weixin_status 查看MCP实际可用性。检查health/ready200、last_error、轮询时间、一个weixin.message订阅和发送终态。原始数据库、附件、QR、profile与日志保持私有。

## 更新

singletrial1 + steadyfuture1 的升级和回退以 [部署与兼容回退](RELIABILITY-DEPLOYMENT.md)、[一次性单来源契约](SINGLE-SOURCE-TRIAL.md) 及 [持续未来来源候选](STEADY-FUTURE-ADMISSION.md) 为准。先离线冻结源码与回退包，确认现场服务、父子进程及锁身份，再按实际授权短停、保留当前状态、替换和重启同一实例。不能用旧 PID、空队列或查询失败证明远端执行者已排空。

保留持续订阅、绑定、凭据、资源限额及所有新数据库增量。正式 close 后核对 `any_new_admission_enabled=false`、`selection_active=false`、`steady_admission_enabled=false`；bulk closed 仍可能允许选中来源或活动 steady 代发送。steady 暂停或 close 之后的新一代只接纳新代启用后首次本地入库来源，不补暂停间隙。已发 permit 和 unknown 不因关闭或重启被撤销，不能重新授权。

旧版兼容回退包尚未针对 steadyfuture1 的**实际生产数据**验收。任何代码切换前先以原稳定 ID 暂停/close steady、读回全部新准入关闭并对账已发许可；离线在同一合成升级数据库验证旧代码失败关闭且正常微信回复可恢复后，才可另审保留新版 weixin、dotjournal、singletrial1 dotcutover 及全部新增代际 guard 的兼容包。不支持退回 0.4.0、整套 localcutover1 或两模块临时组合。不得恢复旧数据库覆盖新消息/许可/回执；保留模块若出问题，保持安全状态修复前进。测试夹具不能作为部署包。

先验证旧工具仍可调用，再核对原插件和目标主 Dot 的新工具目录及实际只读调用，最后更新事件处理提示。历史 final 不补 ack；仅改提示或文档不需要为此重启服务。简单请求直接 final，复杂任务 ack → final，详见 [回复工作流](REPLY-WORKFLOW.md)。

单一 state 目录由 worker.lock 保护；系统托管实例运行时不启动第二个 foreground 或 managed runtime。短停会延迟收发与事件处理，需记录真实影响，不承诺零中断。

## 错误分支

- context_required / fresh_inbound_required：等待本人新的微信来信提供有效context，不伪造或持久假定固定24小时TTL。原send_id保留。
- auth_required / -14：本人按login流程重新确认相同owner；身份改变拒绝。确认ready后保留原发送ID恢复。
- uncertain / delivery_unknown：可能已经发送，查询原ID并等待本人收件证据，不盲重发。重启中断sending也归为不确定；中断CDN准备可安全恢复有限重试。
- upload_expired / attachment_expired：7天媒体缓存已过期，获取本人新的文件；历史发送记录不改写为未发生。
- media_cache_full / queue_busy：等现有任务或TTL释放；不扩大内存/队列上限、不清空历史绕过。
- 订阅验证失败：核对产品实际callback的精确hostname和公开DNS，再更新allowlist；URL/签名不贴公开日志。订阅刷新由调用方负责。

订阅的省略 TTL、有限 TTL 和显式 null 请求必须区分；无到期订阅仍可撤销，且不延长 runtime key 或本地安全截止。部署及回滚边界见[订阅期限契约](SUBSCRIPTION-LIFETIME.md)。支持无到期不代表现有平台已请求或批准无到期，必须核验真实协议结果。

## 幂等、断网和重启

事件执行方先按原 `message_id` 读取并核验 owner、消息内容及 `reply_state`。简单问题只承诺一个 final；复杂任务按当前阶段恢复 ack 和 final，已承诺的文字与 `send_id` 保持不变。微信回复与 Dot 镜像分别记录，镜像的历史 `partial` 或未知结果不阻塞已核验本人消息的幂等微信回复。不能因为事件重投、新会话或重启而换 `send_id` 生成第二条回复。

一次 Dot 镜像许可对应一个稳定来源/阶段/附件 key、一个目标 scope 和一次写入尝试。`authorize` 回包丢失、写入后回包丢失、commit 失败时先查原 journal、原 attempt 和原目标；查不到不等于从未发送。精确目标读回若 `partial=true`、正文或元数据缺失，就停留在未验收状态，不推进该镜像的完成游标，不对相同部分重新授权或换目标补写。无关历史窗口 `partial` 不阻塞新来源第一次自身完备性检查。`committed`/`readback_reported` 和微信侧 `accepted` 也不等于平台独立验证或手机送达。

断网时先保留队列、数据库、订阅和原消息 ID；检查健康、最后错误、订阅期限及原发送状态。网络恢复后用原 ID 做只读查回，再依据状态恢复**可证明未发出**的步骤。`delivery_unknown`、已发 permit 和其他 unknown 仍保持不确定，不能盲重发。CDN 准备失败与消息发送不确定是不同阶段，不把媒体重新上传或改成普通文件发送来掩盖未知结果。

计划重启须在授权范围内确认准确实例、在途发送、两锁和当前准入；正常停止原托管服务或前台隧道，确认 worker 退出后才启动**同一**实例。不要在原实例还在运行时启动第二个消费者，也不要删除锁、恢复旧数据库或用重启代替试验的正式 close。重启后逐项读回绑定者未变、本人有效订阅仍为 1、固定 ACK 设置未变、旧 `send_id` 状态、journal unknown/permit、准入状态、health/ready 与主 Dot 工具。当前关闭候选须回读 `any_new_admission_enabled=false`、`selection_active=false`、`steady_admission_enabled=false`；若已有获准的 active steady 代，重启保留原代际/限额，但已授权或 unknown 绝不重发。正式暂停/close 的间隙来源不自动补镜像，需在验收说明中记录缺口。若进程或锁身份无法确认，保持现状并上报准确状态，不猜测后重试控制动作。

可在隔离临时 state、合成消息和断开的假网络客户端中演练同一 ID 重放、发送超时、网络恢复及重启；这仅验证本地策略。真实断网后的平台读回、订阅续期和手机收件须独立验收。故障回执只记录版本、阶段、稳定 ID 的脱敏关联、错误类别和结果，不复制私人数据库、完整日志、二维码或密钥。

## 卸载和暂停的范围

本项目没有通用的一键卸载脚本。先确定要移除的是 Skill、暂停桥接，还是彻底撤销接入，并只处理已确认属于本实例的对象。三种结果分别记录 Skill、进程、订阅、插件连接、凭据和私有数据的实际状态。

| 范围 | 人工步骤与保留内容 |
|---|---|
| 仅移除 Skill | 核实精确的 `dot-wechat-bridge` Skill 目录，备份其中用户改动，再通过客户端支持方式移除。桥接进程、原订阅、绑定、密钥和数据库不因此停用。 |
| 暂停并保留数据 | 经授权关闭新镜像准入并读回；检查在途发送风险后正常停止准确服务，确认退出和锁释放。保留账本、unknown、绑定与状态。原事件订阅可能继续重试或到期，另行核对；恢复时不重放 unknown。 |
| 彻底撤销 | 分别取得原事件任务/唯一订阅、原插件连接、专用密钥和准确服务定义的处理授权，再核实各自结果。私有数据库和媒体是否保留或删除由本人决定；删除 DB 会失去去重历史。不得连带撤销共享凭据、删除其他项目目录或把历史消息上传审查。 |

已发布许可和远端写入无法靠本地卸载撤销；卸载不等于兼容回退。回退需按 [部署与兼容回退](RELIABILITY-DEPLOYMENT.md) 保留当前数据库、guards、授权历史和 unknown，不用旧快照覆盖。

## Runtime key

创建时记录表单实际到期时间。到期前本人创建替代专用runtime key（Tunnels Read+Use）。Linux 安装后，由本人在最终安装目录的交互式终端，以服务用户身份运行隐藏输入脚本，再只重启本服务：

```sh
sudo -u dotsbridge .venv/bin/python scripts/set-runtime-key.py --replace
sudo systemctl restart dots-wechat-bridge.service
sudo -u dotsbridge .venv/bin/python scripts/safe-status.py --state-dir runtime/state
```

需要具有该服务用户/sudo 操作权限；权限不足时由管理员本人执行，不降低 runtime 的700或密钥的600权限。macOS 前台使用 `.venv/bin/python scripts/set-runtime-key.py --replace`，再停止并重启自己的前台隧道。助手不代输入密钥。先验证新key连接正常和真实MCP读取，再按本人明确授权处理旧key。不要在聊天或命令参数里放secret，不先撤销仍在使用的key。

## 资源

Linux 保持模板unit的CPUQuota100%、MemoryHigh256MiB/MemoryMax384MiB、TasksMax64。读取本service cgroup的memory.current、memory.peak、memory.events；只有实际采样才能报告资源峰值。macOS 前台没有 systemd/cgroup 资源限额，应用文件/块/缓存/队列限制仍有效，应单独测量进程资源。离线resource脚本是小样本，不是压力测试。整机重启、24小时闲置和极限文件仍应按自己的环境验收。

缓存TTL不清理消息数据库历史；依据自己的私有数据保留策略备份/清理。公开Issue只提供版本、固定错误类别和受控统计，不提供个人原文或完整state。
