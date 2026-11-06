# 发布源码

本次 `0.6.0+refactor1` 从已部署命令闭集诊断的源码快照迁入，拆分微信传输、命令与MCP运行职责，并纳入本地 AIHot 来源监控。公开基础提交为 `056cce66dadd83521c279ee309ef204e290ded6b`；具体新文件和哈希由本次导出的 `PUBLIC-BUNDLE-MANIFEST.json` 记录。

既有目标是 [heyxiafei/dots-wechat-bridge](https://github.com/heyxiafei/dots-wechat-bridge)。[RerrentLinden/dots-wechat-bridge](https://github.com/RerrentLinden/dots-wechat-bridge) 是 MIT 上游来源，不是本次上传目标。

## 发布步骤

1. 完成实现与独立审查，保留冻结历史兼容夹具；读取当前目标仓库可见性及实际main提交。
2. 以 `build-public-bundle.py --output` 导出全新目录，按 [VALIDATION.md](VALIDATION.md) 验证实际字节。
3. 人工审查相对当前main的差异，核对来源和许可证。排除生产数据、账号标识、私人路径、密钥、二维码、日志和聊天正文。
4. 从当前main创建独立分支，提交已核验文件并创建可审查的PR。保留无关文件与历史，不直接覆盖默认分支。
5. 读回远端commit/tree及PR，核对文件blob与本地发布包；分别报告CI结果、上传状态和生产部署状态。

只有实际GitHub回执与读回一致时才报告已上传。PR存在不代表已经合并，GitHub CI通过也不代表已部署或手机收件。

## 导出与审计边界

导出器使用逐文件允许清单，不根据目录glob扩大发布范围，不读取运行目录或Git历史。新增文件必须逐项审查后入清单。现有输出不覆盖；symlink、hardlink、未知文件、敏感合成反例和历史夹具变化均拒绝。

`.github/workflows/ci.yml` 是唯一新增的隐藏路径例外。该规则不放行其他workflows、`.env`、`.git`或嵌套隐藏目录。CI只安装公开依赖、导出源码并运行离线检查，不部署、不使用账号密钥、不发送通知。

本机证据、Work交接摘要和运行配置留在开发工作区的私有证据目录，不能混入公开归档。新仓库只纳入安全源码和通用说明；迁移运行数据需要独立的接续方案。

## 许可

保留根MIT许可证、NOTICE、Tencent和wx-bot说明及历史夹具来源。AIHOT署名、项目与线上服务边界见 [AIHOT-PROVENANCE.md](AIHOT-PROVENANCE.md)；上游MIT原文放在 `licenses/AIHOT-MIT.txt`。本项目Python集成不是AIHOT线上重置后端的开源副本，也不重新许可其数据或品牌。

Python依赖、官方tunnel-client由各自发行版提供；本包不捆绑其二进制或私人客户端配置。
