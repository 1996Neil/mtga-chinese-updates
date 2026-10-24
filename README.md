# MTGA 中文维护

macOS + Steam 的《万智牌：竞技场》汉化与中英文牌表导入工具，免费使用。

Windows 用户可查看[其他维护者提供的工具](https://shiqidi.lenitatis.com/mod)。

**[下载更新工具 v3.2](https://github.com/1996Neil/mtga-chinese-updates/releases/download/client-v3.2/MTGA-Chinese-Subscriber-v3.2.zip) · [版本记录](https://github.com/1996Neil/mtga-chinese-updates/releases) · [反馈问题](https://github.com/1996Neil/mtga-chinese-updates/issues/new)**

当前适配：**2026.64.50 / Steam 25919252 / Apple Silicon**。文件与安装恢复检查通过，具体适配及验收范围见版本说明。程序未完成 Apple 公证。

## 安装与更新

1. 下载、解压，双击「安装 MTGA 中文维护.command」。旧版用户也先安装 v3.2，保留相同更新源。
  <img width="488" height="506" alt="Screenshot-8CEAF004-E2AF-43D3-ACDF-B018626AC0F9" src="https://github.com/user-attachments/assets/29906956-1ea3-4141-ab85-dca382dd20f9" />
<img width="494" height="452" alt="Screenshot-4DE87B4F-EDBA-45ED-8EF9-F30084CA8C75" src="https://github.com/user-attachments/assets/9e973bdc-45d2-4054-b8fb-1a55671b1534" />
<img width="1450" height="1150" alt="Screenshot-E3525FC6-9ED7-4A51-9C7C-01582457BBF9" src="https://github.com/user-attachments/assets/00f7eefb-2c19-45bb-b4c6-ff754e55db70" />
再次同意，之后再运行一次，MTGA 中文维护.command就出现在桌面了。
3. 完成 Steam 和游戏内下载，退出 MTGA。
4. 打开桌面「MTGA 订阅更新」：**检查更新 → 预演 → 应用更新**。游戏语言保持 **English**。

其他来源的旧汉化也可直接更新：先备份，再覆盖匹配的新版资源。

以后资源更新直接打开桌面工具，无需重复安装。没有匹配版本或校验失败时会停止；恢复入口用于还原上次更新前的状态。无需 Git、Python 或 Codex。

## 订阅与支持

欢迎右上角 **Star** 支持维护。接收新版邮件：**Watch → Custom → Releases**，并开启 GitHub 邮件通知。Star 不等于邮件订阅，未订阅也可主动检查更新。

## 自行维护

[完整使用说明](tools/chinese-maintainer/SUBSCRIBER_FLOW.md) · [构建与贡献](docs/DEVELOPMENT.md) · [汉化来源规范](tools/chinese-maintainer/SOURCE_LOOKUP.md) · [Agent 入口](AGENTS.md)

可复用的译文和具体来源索引见 [精简维护数据](maintenance-data/README.md)。普通玩家更新汉化不需要下载维护数据或证据包。

源码、更新工具和监控均在本仓库；官方、社区及 AI 译文分别记录来源。社区项目，与 Wizards of the Coast 或 Steam 无关联。[源码许可与第三方内容边界](THIRD_PARTY_NOTICES.md)。
