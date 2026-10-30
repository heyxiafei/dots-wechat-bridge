# 有界长图分片：候选契约

此文件描述本版分页契约，实际部署状态以本次维护验收记录为准。它只改善已成功缓存、通过现行校验的图片预览；不重下载旧失败附件、不重放事件、不发送微信。

## 使用

`read_attachment_image(message_id, attachment_id)` 保持原总览行为及JPEG编码。总览只适合了解布局；极长图缩到2048框会成为窄条，不能据此声称文字已看清。

新增可选 `page`，整数0..63、从0开始、按行优先。一次返回一张原比例区域JPEG及原附件元数据。首先读取 `page_count`、`pagination_available`、`page_grid` 和 `source_box`；再按需要查看对应页面，覆盖所需页面后才能声称那些内容已读。不能自动发消息或重排旧事件来取得图片。

分片边长最多2048像素，步长1984，邻页重叠64；页数最多64。超64页时仍可取兼容总览，但 `pagination_available=false`、`page_count/page_grid=null`，显式分页报 `page_limit_error`。超范围页报 `image_page_out_of_range`。横图和双轴图片同样使用有限网格。EXIF转正后计算区域坐标。

每页 `original_sha256` 对应缓存原文件，`preview_sha256` 对应本次JPEG；`source_dimensions/source_box/preview_dimensions` 区分原比例区域和派生预览。预览去EXIF、为有损JPEG，`is_original=false`、`is_complete_source=false`、`first_frame_only=true`。动图不代表所有帧已读。需要原字节时只能使用已有只读chunk/block工具并核验完整SHA；不能把预览当作原图。

## 安全与资源

原图仍限20MiB、800万像素及既有允许格式，Pillow解压炸弹检查保留。分页不会绕过入站失败、owner绑定、7天TTL、SHA、缓存状态及文件描述符身份校验；解码完成后再次核验绑定和元数据。只读路径不更新DB或缓存，不改变发送器、幂等、订阅和ACK。

原字节经有界FD读取及SHA校验，不再先将完整原文件Base64编码再解码。输出JPEG写入时限制4MiB，完整RPC限制8MiB；实际平台传输限额尚需上线后的现有工具验收，不能由本地上限推断平台一定接受。

Pillow仍完整解码允许的原图，再EXIF转正、RGB转换与crop；不是流式解码。source、转正副本、RGB副本、crop与BytesIO均显式关闭。`full_decode_required=true`、`rss_budget_enforced=false` 如实说明没有单次解码的硬RSS预算。128MiB是共享磁盘缓存上限，不是内存限额。没有放宽8M或bomb保护来支持更大原图。

## 故障信息

诊断补丁保留旧 `invalid_or_oversize_image`，仅在现有 `provenance.image_validation` 中记录白名单reason/stage、可解析尺寸/格式及观察到的明文字节数，不保存异常字符串、URL、密钥、原图或聊天正文。下载末尾合法Content-Length不匹配报终态 `media_download_incomplete`，不因此自动重试。

旧失败若缺少原始缓存、尺寸或细分信息，不能确定是像素超限还是损坏/截断等原因。dot重传小副本不能反推微信原图。新诊断只服务未来自然入站；不会给旧记录补造证据。

## 上线与回滚条件

获得具体维护授权并核实实例后，按原项目流程备份原DB和运行文件、核对生产基线。分页变更涉及 `probe/media.py`、`probe/probe.py`、`docs/mcp-schema.json`；整体升级必须按版本清单成组替换并验证全部依赖，不能只复制这三个文件冒充完整 R7。重启仅针对已确认的原服务。

保持原发送ID、unknown状态、所有既有订阅、权限和凭据；重启后核验此前已获准启用的消费者；若generation要求维护恢复，仅按既有原授权/原cutoff核验恢复，不新切点、不重放历史。刷新既有工具后必须实际核验 `page` schema及调用可达，不能用“点击刷新”代替证据。任何结果未知先读回，不重复维护。

回滚按所升级版本的完整清单成组恢复备份源码并按获批维护范围处理原服务，不回滚生产DB、不清出站、不重发。真实微信可读性尚未验收；需要未来自然新图或另行获准的单次真实验收。若只为还原旧失败根因，最小补充是可信原尺寸/格式或已合法落地的未缩小原文件，不要求反复重传。
