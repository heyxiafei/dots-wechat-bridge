# AIHOT 集成来源与许可

核实日期：2026-11-04。

本项目的 Codex 重置通知使用 [AIHOT](https://aihot.news/) 提供的公开数据。AIHOT 官网明确署名为 **数字生命卡兹克**；其开源仓库为 [KKKKhazix/AIHOT](https://github.com/KKKKhazix/AIHOT)。本项目在此致谢并保留来源链接，不表示与 AIHOT 存在官方合作或背书。[官网作者介绍](https://aihot.news/about)

## 本仓库集成的内容

- `integrations/aihot/`：本地 Python 采集与通知账本代码，来自本项目既有的独立监控工具。
- `probe/aihot_notice_consumer/` 与 `probe/aihot_email.py`：微信桥一侧的通知消费、授权校验、去重与邮件交接。
- 数据入口：[`/api/v1/codex-resets/recent`](https://aihot.news/api/v1/codex-resets/recent)。按上游文档使用匿名只读查询、每 600 秒轮询及 ETag 条件请求；协议以 [OpenAPI](https://aihot.news/openapi-v1.json) 和 [Agent 接入页](https://aihot.news/agent?tab=api) 为准。[官方机器入口说明](https://aihot.news/llms.txt)

这些模块接入 AIHOT 服务，不是 AIHOT 引擎的完整 fork。此次合入没有从上游引擎导入实现文件、Logo、字体或运营数据库。上游 README 明确说明 Codex 重置监控等专属功能保留在线上站点，不能把公开引擎当作该后端的开源副本。[上游说明](https://github.com/KKKKhazix/AIHOT/blob/cd424b0a513c7c8535b6f291b106a3086d6b75bf/README.md#说在前面)

通知中的“预告”“来源确认”表示来源报告的状态，不表示接收者本人账户额度已经重置。保留来源与原帖链接；个人额度需另外通过本人账户的官方接口核实。

## 上游代码许可与服务规则

核对的上游版本：[`cd424b0a513c7c8535b6f291b106a3086d6b75bf`](https://github.com/KKKKhazix/AIHOT/tree/cd424b0a513c7c8535b6f291b106a3086d6b75bf)。

上游代码采用 MIT，版权标注为 `Copyright (c) 2026 数字生命卡兹克`。原许可文本保留在 [licenses/AIHOT-MIT.txt](../licenses/AIHOT-MIT.txt)，用于准确记录上游出处；它不把本项目的 Python 代码归为该作者作品，也不把线上服务或数据重新许可为 MIT。[上游 LICENSE](https://github.com/KKKKhazix/AIHOT/blob/cd424b0a513c7c8535b6f291b106a3086d6b75bf/LICENSE)

上游 README 和 NOTICE 明确将 AIHOT 名称与 Logo 排除在 MIT 许可之外。本项目仅在说明数据来源时提及 AIHOT，不使用其 Logo，也不以 AIHOT 命名自己的独立站点。上游字体另有许可，本次没有引入字体资产。[上游 NOTICE](https://github.com/KKKKhazix/AIHOT/blob/cd424b0a513c7c8535b6f291b106a3086d6b75bf/NOTICE)

线上服务另受 [AIHOT 公开使用规则](https://aihot.news/terms)约束。核实时版本为 1.1，生效日期为 2026-09-30：个人非商业、公益非商业和组织内部用途可免费使用；对外商业服务、数据转售和公开镜像等用途需要上游书面授权。发布本地接入代码不包含第三方数据的再许可。保留响应中的来源、canonical 与权利信息，遵守缓存、限流、更正和删除约定。

