# MTGA 中文维护与双语导入

面向 **macOS + Steam 的《万智牌：竞技场》**：安装一次更新工具，以后主动检查、预演并应用维护者发布的适配汉化。维护源码、订阅客户端、更新监控和使用说明集中在这个仓库。
windows用户可以去这个网站下载汉化工具https://shiqidi.lenitatis.com/mod

**[下载安装包 v2.1](https://github.com/1996Neil/mtga-chinese-updates/releases/download/mtga-2026.64.0-25763111-r1/MTGA-Chinese-Subscriber-v2.1.zip) · [查看发布](https://github.com/1996Neil/mtga-chinese-updates/releases) · [反馈问题](https://github.com/1996Neil/mtga-chinese-updates/issues/new)**

觉得有用，欢迎点右上角 **Star** 支持。想收到可用更新的邮件，请 **Watch → Custom → Releases**，并在 GitHub 通知设置为 Watching 开启 Email。

## 第一次安装

1. 下载 ZIP，解压后双击「安装 MTGA 中文维护.command」。
2. 等 Steam 更新完成，打开 MTGA 完成游戏内下载，然后完全退出游戏。
3. 打开桌面「MTGA 订阅更新.command」，选择「检查更新」。
4. 找到匹配版本后选择「预演」：下载、重建并校验资源，这一步不覆盖游戏。
5. 预演成功且游戏已退出后，选择「应用更新」；完成后启动游戏，游戏语言保持 **English**。

普通用户不需要 Git、Python 或 Codex。下载与更新无需 GitHub 账号；订阅邮件需要免费账号。

## 以后收到新版本邮件

**如有游戏更新，先完成 Steam 和游戏内下载 → 退出游戏 → 桌面更新工具 → 检查更新 → 预演 → 应用更新 → 启动游戏。**

普通汉化修订不需要重装客户端，也不用 `git pull`。邮件只提醒有新发布，不会自动改游戏。工具按本机版本判断能否使用；没有匹配版本就等待适配，不强装其他版本。

- 下载中断可以重新执行，v2.1 支持断点和已校验差分缓存。
- 版本、签名、文件状态或预演检查失败会停止，不绕过检查。
- 更新前保存备份；恢复以工具记录为准。Steam 再次更新后不能任意恢复旧文件。
- 更新工具本身或协议升级另发安装包；普通资源更新不替换程序。

完整步骤与故障处理见 [用户更新流程](tools/chinese-maintainer/SUBSCRIBER_FLOW.md)。

## 当前发布范围

- 现有公开客户端：**v2.1**；现有公开资源：**2026.64.0 / Steam build 25763111，r1/r2，testing 渠道**。以客户端兼容检查与发布记录为准，这不表示适用于任意新游戏版本。
- 二进制含 Apple Silicon 与 Intel；当前实际资源开放范围为已验证的 Apple Silicon 组合，Intel 游戏验收尚未完成。
- 程序为 ad-hoc 签名，尚无 Developer ID 签名及公证，不能保证所有新电脑直接打开。若 macOS 拦截，请反馈，不要关闭系统安全检查。
- 本次仓库整合没有发布新汉化。最新本地来源复查候选尚未完成本次游戏运行验收。
- 旧 r1/r2 的共用预发布条目保留原样；以后的新修订采用独立 Release，并在签名索引就绪后发布通知。

## 想自己维护或贡献

Fork 或拉取本仓库，按 [开发与构建说明](docs/DEVELOPMENT.md) 操作。普通安装流程与开发环境分开。

| 目录 | 职责 |
|---|---|
| `tools/chinese-maintainer/` | 官方底稿检查、译文来源核对、候选构建、字体、双语导入与安装事务 |
| `tools/subscriber/` | 原生订阅客户端、差分与签名协议、发布器和回滚 |
| `mtga-watch/` | 官方公告、Steam 和已知资源变化监控；新事件提醒维护者 |
| `feed/`、`download.json` | 已安装客户端读取的原有签名更新入口 |
| `tools/card-name-map/`、`tools/card-overlay/` | 可选本地查牌与读牌工具源码 |

维护前先读 [Agent 入口](AGENTS.md) 和 [汉化来源查询规范](tools/chinese-maintainer/SOURCE_LOOKUP.md)。现成中文优先核对大学院废墟、对应中文牌面及大陆官方资料；社区译文与 AI 文案明确标注，没有官方全文的客户端文案可以保留经过核对的 AI 表达。

维护过程中的公告和资源变化提醒与用户可用版本分开。普通用户只订阅 Releases；维护者查看 Actions 或维护事件。详见 [监控说明](mtga-watch/README.md)、[仓库迁移记录](docs/MIGRATION.md) 和 [开发说明](docs/DEVELOPMENT.md)。

本项目是社区维护工具，与 Wizards of the Coast 或 Steam 无关联。源码使用范围及第三方内容边界见 [许可证](LICENSE) 与 [第三方说明](THIRD_PARTY_NOTICES.md)。源码许可不授予游戏资源、卡牌或字体的再分发权限。
