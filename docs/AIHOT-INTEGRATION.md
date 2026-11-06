# AIHOT 采集与桥接集成

`integrations/aihot` 收集 AIHOT 公开的 Codex 重置公告，验证来源与时间窗，产生本地通知意图，再由现有桥接消费者完成获准的投递。它是独立编写的本地集成器，不是上游 AIHOT 网站的整包副本或官方客户端。上游项目为数字生命卡兹克公开的 [KKKKhazix/AIHOT](https://github.com/KKKKhazix/AIHOT)，网站为 [aihot.news](https://aihot.news/)。作者核验与集成区别见 [来源记录](AIHOT-PROVENANCE.md)，许可证见项目 [NOTICE](../NOTICE.md)。

安装、导入模块和离线测试都不会启用采集或通知。采集器默认关闭；不运行模型，不自动消耗或重置 Codex 额度，不包含私人任务清单、收件人、凭据或服务配置。

## 数据流与模块边界

```text
AIHOT public /api/v1/codex-resets/recent
  → monitor.py：有界 GET、完整快照验证、时间窗/来源判定
  → notice_relay/source_adapter.py：通知资格、来源版本、终止水位
  → 私有 dual-notifications.json：未来事件 + 每通道稳定请求/回执
  → probe/aihot_notice_consumer：微信消费者
  → probe/aihot_email.py：邮件事件与获准宿主交接
```

采集器只有匿名出站 GET：固定 HTTPS endpoint、25 秒超时、最多 1 MiB 响应，不继承代理认证、不发送 cookie、不跟随重定向。默认采集间隔 600 秒，快照核验新鲜度 1800 秒；HTTP 304 不延长来源已核时间。错误按原间隔和 Retry-After 有界退避；重复边缘拒绝会停止该访问路径，不改身份或换源重试。

`monitor.py` 和 `source_adapter.py` 负责采集端业务。`integrations/aihot/notice_relay/{relay,store_helpers,text_b}.py` 显式复用桥接现有模块，令生产者和消费者使用同一账本格式、锁、原子写入、稳定请求 ID 与通知格式，避免维护两个分叉。完整仓库是该集成的运行单位，不应单独复制这几个包装模块部署。

采集器没有微信或 Gmail 发送接口，不会通过 HTTP 代理或新后台任务旁路现有协调者。邮件内容构造和记录依赖可信宿主显式提供的 `AihotOperatorConfig`，没有默认收件人；配置对象不等于平台批准。微信 owner、原订阅、批准、身份代际、硬停止和并发门槛仍由消费者核验。

## 保留的事件语义

- 首次有效响应建立历史基线；只处理基线和启用切点之后的未来事件。暂停后恢复重新建基线，不补发暂停期间或迁移前的公告。
- `announced` 与来源确认的 `confirmed` 分开；预计完成、到账猜测、含糊时间窗、来源冲突、withdrawn/unknown、过期或不健康快照不会升级为已确认通知。
- `reset_credit` 只在严格已知的非通知结构下跳过；未知类型和矛盾字段仍停止该快照，不能一概忽略。
- 事件版本与活动时间分开保存。倒序、同水位冲突、撤回/确认终止水位、已处理旧版本不会重新激活。
- 微信与邮件各有稳定请求、尝试状态和原 provider ID。`attempting`、`uncertain`、`accepted` 不以新 ID 重发；空查询不证明未发送。API 接受与本人收件分别记录。
- 保留原通知正文与格式的不可变承诺。B 风格展示缺失或变化不能替换已经承诺的文字。

上游公告只说明公开事件，不提供个人实时额度。此集成不建立任何自动模型任务，也不以 token 数、固定运行分钟数或旧额度快照替代个人额度核验。

## 显式配置与手动接续

以下是部署接口说明，本仓库不会执行这些操作或安装定时器。已有实例必须先核实唯一生产者、消费者和原状态的对应关系；不要在已运行的同一数据目录创建第二个写入者，也不要把公开发行目录当私有数据目录。

使用安装的 Python，从项目根目录运行模块，并显式给出本人选择的私有目录：

```sh
python -m integrations.aihot.monitor status --data-dir /absolute/private/aihot-data
```

`--data-dir` 必填。数据目录应属于当前服务用户、权限 `0700`，持久文件保留 `0600`。状态、控制、锁和日志拒绝符号链接、硬链接及非私有文件；不会为了继续运行去改动链接目标的权限。缺少 `control.json` 时采集关闭；缺少 `dual-control.json` 时双通道意图捕获关闭。通用关闭模板分别位于 [aihot-control.json.example](../config/aihot-control.json.example) 和 [aihot-dual-control.json.example](../config/aihot-dual-control.json.example)。它们不包含收件人或任何凭据。

在部署目标、状态接续和采集范围已经明确后，以下命令分别改变本地采集开关与意图捕获开关：

```sh
python -m integrations.aihot.monitor on --data-dir /absolute/private/aihot-data
python -m integrations.aihot.monitor dual-capture-on --data-dir /absolute/private/aihot-data
python -m integrations.aihot.monitor poll --data-dir /absolute/private/aihot-data
```

最后一条才执行一次公开 API 采集；前两条不发送网络请求或微信/邮件。`on` 不会开启模型派发，`dual-capture-on` 不会 arm 桥接消费者。持续采集需要部署方安排原有或明确选择的调度器；本集成不创建 listener、LaunchAgent、systemd 服务或云端自动化。电脑休眠时不能保证每十分钟采集。

暂停使用同一目录的 `off` 和 `dual-capture-off`。查看聚合状态使用 `status`、`dual-status`。保留 `state.json`、来源水位、通知账本及既有 unknown；不要复制旧备份覆盖期间新增的账本，不通过 off/on 补旧提醒。历史单通道兼容命令仍无发送器，不作为新增发送路径。

接入桥接时，应由获准宿主显式设置消费者所使用的同一私有账本目录及 `AihotOperatorConfig`，并核验当前 owner、邮件路由/授权、原订阅、身份代际与截止。不存在“读到一个配置文件就自动授权”的路径。对于旧邮件 OAuth 代际或未知提交，保持停止并查询原动作；不自动重新 arm 或重放。

## 功能状态与验收

|能力|源码状态|部署验收要求|
|---|---|---|
|公开快照采集、源与窗口判定|已纳入可离线测试的集成|实际 endpoint/schema 可用、定时器运行和来源新鲜度独立核验|
|双通道意图捕获与共享账本|已实现，默认关闭|唯一生产者/消费者、数据权限、原水位和停止条件|
|微信 B 风格通知|复用现有消费者与格式器|获准未来自然事件、一次提交、本人手机收到|
|邮件通知|保留现有宿主交接契约，默认没有收件配置|当前邮件身份/授权、原请求关联、发送读回与本人收件|
|暂停、恢复、去重、未知状态保留|合成回归覆盖|受控维护与真实重启验证，不能用测试数量代替|
|自动模型任务/自动消耗或重置额度|未实现，始终关闭|本集成不提供该功能|

全套离线验证仍使用项目入口：

```sh
python -B -m unittest discover -s probe_tests
python -B scripts/generate-schema.py --check
```

生产者的历史合成回归通过 `probe_tests/test_aihot_producer.py` 纳入该入口；配置默认关闭也有独立回归。测试使用临时目录、合成公告和 fake transport，不读取真实账本、不调用公开 API、不发送微信或邮件。真实安装、采集、投递和收件必须分别记录，不把本地源码存在或离线通过称为已恢复线上 AIHOT。
