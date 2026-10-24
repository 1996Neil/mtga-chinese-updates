# MTGA Chinese Patch & Bilingual Import Maintainer v1

模式：实现、验证。

目标：以每次新版官方资源为底稿，联合维护中文显示与原生双语导入。首迁移 2026.63.20。
约束：sources、历史快照/manifest/donor 只读；不覆盖旧 DB、不改官方 manifest、不改可执行文件/账户/收藏；不自动猜翻；未知原文/用途/格式保留英文。
修改范围：本 tools/chinese-maintainer、bin/mtga-cn、可选本机命令入口；游戏文件仅在所有安装门槛通过且已退出游戏后安装。
完成条件：registry、diff、五层候选、独立校验、英文/中文/混合固定回归、分项报告、精确备份与可恢复安装事务；客户端结果须有当前版本证据，历史 PASS 不移用。
当前步骤：核查官方资源和来源 → 构建 registry 与候选 → 独立审查与回归 → 安装门槛与客户端验收。
风险：数据库解析不证明原生 importer；字体的静态 glyph/CRC 不证明真实渲染；官方更新漂移必须拒绝安装/回滚。
