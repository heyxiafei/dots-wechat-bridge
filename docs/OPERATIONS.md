# 更新、恢复与到期

## 每次检查

在最终安装目录操作。Linux 安装后的 runtime 属于 dotsbridge，目录权限700，不向普通登录用户开放。以服务用户运行只读状态脚本：

```sh
cd /opt/dots-wechat-bridge
sudo -u dotsbridge .venv/bin/python scripts/safe-status.py --state-dir runtime/state
systemctl show dots-wechat-bridge.service --property=ActiveState,SubState,UnitFileState,MemoryCurrent,MemoryMax,CPUQuotaPerSecUSec
```

macOS 前台部署使用 `.venv/bin/python scripts/safe-status.py --state-dir runtime/state`，无须服务用户或 systemd。工具 get_weixin_status 查看MCP实际可用性。检查health/ready200、last_error、轮询时间、一个weixin.message订阅和发送终态。原始数据库、附件、QR、profile与日志保持私有。

`safe-status.py` 明确输出 `scope=local_snapshot`、宿主墙钟observed_at和 `remote_host_mapping=unknown`。它只观察指定本地库与配置的127.0.0.1健康端点；即使两者返回200，也未证明这是当前线上App/Tunnel的真实host。healthz和readyz分别有界读取，单端失败不抹掉另一端的HTTP结果。poll_age为观测差值，不设推测的“在线”阈值；缺失、异常或未来时间保留未知。

运行主机可连接、微信事件回调到达、Tunnel客户端poll、MCP请求返回是不同证据；任何一项恢复都不能代替其余核验。事件到达后MCP返回客户端未连接错误，只证明该请求入队后未被客户端取走，不能据此重启某个旧库对应的服务。实际host/service未映射时先索取最后poll、client/session和受支持的运行控制证据；本章的停止/恢复步骤只有在对象已核实且该动作获准后才适用。

## 分阶段时序

复用 `transport_timing` 的入库观察/提交、event排队、webhook尝试/HTTP接受、首次精确读取、final排队/POST/API接受记录；缺阶段保持null，不填0或用其他阶段代替。墙钟倒退/阶段重叠不能强行整理成正耗时。读取工作用单调时钟度量，但不含工具传输、结果序列化或平台调度。HTTP接受不是模型开始，accepted不是手机收到；20秒目标需同一消息各时点及本人收件证据，不能从一条样本推导SLA。

## 更新

公开 CLI 默认关闭 AIHot。已有启用实例须在停旧服务前核实原路由、批准、数据目录、cutoff、未知记录，并准备可信宿主的配置注入及同一消费者恢复验证；未满足时保持原实例，不能以未改写旧配置声称迁移成功。详见[安装接续](INSTALLATION.md)。

先在独立候选目录跑测试和schema检查。更新源码前确保没有sending/preparing。备份原源码并使用SQLite自身backup机制保存私有一致快照，保留当前state/secrets/profile。只停本服务，切换代码后启动，验证health/MCP/计数保留，再在既有插件设置刷新工具。服务暂停时读操作可重试；写操作始终用原稳定ID查状态。

0.5.0 的 ack/final 复用0.4.0已有 outbox 字段，仅新增可重建的索引；不改写历史消息、最终回复身份或订阅。先部署、验证旧工具仍可调用，再在原插件 Refresh tools，并确认新工具真实可调用，最后迁移原自动化提示，不能在工具尚未接通时切换。历史 final 无需补 ack。

事件处理提示按 [回复工作流](REPLY-WORKFLOW.md) 分支：简单请求用旧工具直接 final，复杂任务才 ack → final。此选择由调用方完成，无须修改 Tunnel profile 或回复服务器的配置。若只更新这类提示或教程，桥接0.5.0代码未变，不必重启生产服务。不要用新工具 `phase:"final"` 跳过它的 ack 前置条件。

回滚到0.4.0前停止新两阶段承诺，确认所有 reply_ack 与其 final 均已终态；旧 worker 不懂阶段等待，不得让它处理未终态的两阶段记录。回滚只换代码，不恢复旧数据库覆盖期间新消息。部署准备阶段的自动回滚必须核对没有新增 phased commitments；若有，保持0.5.0源码与私有快照并报告故障，不启动旧 worker。

单一state目录由worker.lock保护。systemd运行时不再启动第二个foreground或managed runtime。对服务做回滚时先检查新旧数据库/队列格式；旧版不能识别媒体字段时，不让它处理媒体待发记录。恢复代码不自动恢复旧数据库，以免丢失更新期间收到的消息。

## 错误分支

发送诊断与重试决策分别解释：HTTP401/403/429/5xx明确收到响应时，api_result保留response_received=true和http_status；这不表示业务已拒绝且必未发送，终态仍按原保守delivery_unknown处理，不自动重发。timeout/network错误不能伪称收到HTTP响应。平台审批拒绝是另一层，持久停止、不换接口。

已解析的业务结果若含-2和其他明确非零拒绝码，先记terminal rejected，避免-2掩盖拒绝。仅明确的 `rate limit` / `rate limited` 沿原同ID最多3次规则；空或未识别的-2错误文本不再猜成限流重试。`prepare failed` / literal `unknown error` 保留既有context_required等待新来信语义，这只是原项目处理策略，不证明其所有上游根因或固定过期时长。新来信不能重排已rejected、denied或delivery_unknown的旧发送。

- context_required / fresh_inbound_required：等待本人新的微信来信提供有效context，不伪造或持久假定固定24小时TTL。原send_id保留。
- auth_required / -14：先停止准确的原 worker 并确认 `worker.lock` 释放，本人在同一 state 目录按login流程重新确认相同owner；身份改变拒绝。确认ready后启动原 worker，保留原发送ID恢复。独立 login 进程不得在 worker 运行时改绑；若 AIHot consumer 曾启用，重启后须核同一本人和原路由。同 owner/route 且连续启用的受检维护恢复保留原 cutoff；首次启用或明确 off 后再启用才按原契约建立未来切点，不补历史。
- uncertain / delivery_unknown：可能已经发送，查询原ID并等待本人收件证据，不盲重发。重启中断sending也归为不确定；中断CDN准备可安全恢复有限重试。
- upload_expired / attachment_expired：7天媒体缓存已过期，获取本人新的文件；历史发送记录不改写为未发生。
- media_cache_full / queue_busy：等现有任务或TTL释放；不扩大内存/队列上限、不清空历史绕过。
- 订阅验证失败：核对产品实际callback的精确hostname和公开DNS，再更新allowlist；URL/签名不贴公开日志。订阅刷新由调用方负责。

订阅的省略 TTL、有限 TTL 和显式 null 请求必须区分；无到期订阅仍可撤销，且不延长 runtime key 或本地安全截止。部署及回滚边界见[订阅期限契约](SUBSCRIPTION-LIFETIME.md)。支持无到期不代表现有平台已请求或批准无到期，必须核验真实协议结果。

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
