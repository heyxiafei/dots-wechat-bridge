# 本人请求的模型成果交付

适用：已核验绑定本人明确请求制作并交付的非敏感模型成果。授权限于该次请求、约定成果和预览图；不能从本说明推出任意主动通知、他人收件、敏感资料外发或新权限。原请求的可信证据需在私有交付清单中保存，不把聊天正文/真实 ID 放入公开源码。

## 必须完成的交付顺序

1. 建立交付清单：记录本人 request_ref、owner_ref、成果及版本 artifact_key、每张待交付预览 attachment_key、实际文件大小/格式/SHA256。明确私有 Drive 存档范围与目标，不擅自开启公开链接。每张必要预览都列为待交付，不能只记一句“以后发图”。
2. 先保存到已经获准的私有 Google Drive，再通过正式工具核实文件 ID、位置、权限、大小及内容完整性。为避免把同名旧文件当新成果，保留实际 hash 或可验证的下载内容哈希；不能凭链接存在断言已核实。源文件与每张预览分别记录存档证据。
3. 存档核实后，**必须将每张预览图以原生图片发回绑定本人微信**。Drive 链接是补充，不能替代图片；.blend 等手机不便查看的源文件留在 Drive，仅按需要或本人明确要求发送，不自动上传所有源文件到微信。
4. 使用现有 [媒体契约](MEDIA.md) 上传真实字节并核 size/hash、取得 ready。复用既有 send_weixin_media 和幂等发送路径，不新造发送器。最新本人消息仅用于合法上下文更新；授权仍关联原制作交付请求，不能把任意最新来信当交付批准。
5. 存档、图片上传、发送承诺、API 接受、本人实际收到独立记账。queued 只表示排队，accepted 只表示接口接受，delivered 保留未知；只有本人明确收件证据才记录实际收到。多张图分别核验，不能由一张收到推断全部收到。

## 文字 final 与成果附件

明确要求的 PDF、Word 必须发原文件，不能以截图或预览代替；视频在已授权范围内使用原生video路径。`workflow_contract.attachment_plan` 区分preview_image、requested_document、requested_video、model_source；每项仍需可信父协调者核验授权与实际字节。BLEND默认只提供私人Drive入口，明确要求源文件时才走file。原生协议和完整hash通过不等于手机能打开/播放。

授权交接须按 [真实授权证据传递](AUTHORIZATION-HANDOFF.md)。本地scope匹配不授予平台许可；真实审批拒绝用DeliveryJournal.record_denial持久保存，即使之前claim已开始也不能丢失拒绝状态。无自动清除/换键/通知兜底；已存在send_id时不能改称确定未执行。

进度文字不应占用唯一 final；复杂工作按原阶段规则处理。若历史进度已经占用 final，保留原承诺，不改写或再建文字 final。本人已授权的待交付成果图片仍是该请求的明确交付义务，可按清单走独立媒体路径；它不因此变成无限制主动通知，也不能通过 notification 工具绕过媒体权限或审批拒绝。权限检查拒绝时停止并说明具体动作；新的相关授权且确定未执行时才按规则重试，未知结果先查原记录。

## 私有交付清单与幂等

每张预览使用 (owner_ref, request_ref, artifact_key, attachment_key) 派生稳定键，SHA256 是不可变内容承诺。重试不能换键；相同键不同字节视为冲突，新版本需明确新的成果版本。保存调用前稳定 upload key / send key，以及工具返回的真实 upload_id / send_id，两者不能混用。

实际操作前由唯一交付协调者持久记录意图；多协调者需原子 claim，不能并发提交。DeliveryJournal 提供调用者传入 SQLite 连接上的持久登记、内容冲突拒绝和 compare-and-swap 单次提交 claim；不自动发现生产库，也不具备自动发送能力。持久状态示意：commitment、archive(source/previews 的 file_id/hash/verification)、upload_id/state、submission_started、send_id、API 状态、owner_received_evidence_ref。记录不含令牌、私有下载 URL 或完整原请求正文。

一旦提交开始，即使超时且 send_id 未返回，也保留原键并查原动作；空查询不代表未执行。已有 send_id 的 queued/accepted/uncertain/rejected 均先查原记录，不能自动新建发送；内容/大小变更、上传过期和权限拒绝各自处理，不借机补发历史。崩溃恢复只恢复交付清单，不重放未知图片，不承诺严格 exactly once。

## 离线契约与验收边界

`probe.deliverable_contract.next_step` 仅验证给定证据并建议下一步，不访问外部服务。DeliveryJournal 仅对调用者明确提供的本地 SQLite 连接记账；目前尚未接入生产协调者或 MCP 工具，不访问 Drive、微信或网络。调用方必须提供真实已核实证据，布尔值本身不是授权凭证。它对每张预览要求自身私有 Drive hash 读回；整个成果交付还需清单中所有约定源文件的存档核实。

离线测试覆盖链接不替代核实、缺授权/敏感/非私有失败关闭、图片上传状态、final 已占用仍保留成果交付、每图独立键、内容冲突、未知发送和无 ID 超时、API 接受不等于收件、拒绝优先停止。测试使用合成数据，不产生真实通知。

本轮只修订本地候选。当前 MCP 在线实例与本机目录尚未对应；本说明尚未部署到线上协调者，也未推 GitHub，不能声称真实成果已经交付。
