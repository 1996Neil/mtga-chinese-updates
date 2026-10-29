# MTGA 中文维护：订阅客户端与发布端

使用者安装一次 macOS 原生客户端，之后检查并应用维护者发布的适配资源，不需要 Codex、Python、Git 或发布者的历史备份。新游戏版本仍需要维护者运行构建、审查变化和实测，然后发布签名资源；已有翻译不会自动保证新格式兼容。

首版范围：macOS 13+、Steam MTGA、五层数据资源。客户端含 arm64/x86_64；当前本机真实资源验证为 Apple Silicon。Intel 编译不等于 Intel 游戏内实测。首版由用户打开中文菜单检查更新，只有选择“安装或更新汉化”才修改游戏；没有后台定时替换，也没有客户端自升级。

## 使用者

1. 安装维护者提供的安装包，双击“安装 MTGA 中文维护.command”。
2. 此后打开用户 `Library/Application Support/MTGAChineseSubscriber/MTGA 中文维护.command`。
3. “检查更新”找到适合本机 Steam build 的版本；“下载并检查”下载、重建和校验五层文件，不修改游戏。
4. 完全退出游戏，再选择“安装或更新汉化”。更新成功后检查显示与导入。
5. “撤销上次更新”回到上次更新前的实际状态。中断操作也可用恢复处理；若 Steam 已更新到另一版本，客户端拒绝用旧文件覆盖。

没有匹配版本时等待维护者发布。不同文件布局、损坏备份、签名失败、过期索引都停止操作。测试渠道不接受作为稳定验收证明。本地测试演示包使用 `MTGAChineseSubscriber-LocalTest`，必须保留解压目录；它不是联网订阅源。

已有 testing 安装可在更新地址、公钥均相同且 stable 索引验签及有效期通过后，由安装器迁移到 stable。迁移保留安装事务、资源缓存、备份及回滚记录，并保存原 profile/trust 配置；中途失败可重跑安装器。不同地址、公钥、stable → testing 或未完成安装事务均不自动迁移。

遇到其他来源的旧汉化时，优先使用已保存并校验通过的官方底稿重建；缺少底稿则下载签名完整资源覆盖对应层。覆盖前备份当前实际文件，恢复会返回覆盖前状态。已匹配的层不重复下载，后续缺少底稿时仍可使用完整资源；不将第三方汉化当成官方底稿。主程序、动态库或官方清单被改动时仍停止。

独立命令：`check`、`preview`、`update`、`status`、`rollback`、`recover`。自定义 Steam 库可传 `--steam-library <包含 steamapps 的目录>`。状态目录可传 `--state`，不同状态对同一游戏共用 inode/device 锁（当前 macOS 用户范围）。不要在安装过程中启动游戏或在 Steam 发起文件验证。

## 维护者如何更新

流程：**获取新官方资源 → 核对底稿 → 生成汉化候选 → 静态验证与独立审查 → 真实游戏验收 → 签名包 → HTTPS 发布**。

- 既有 Raw 翻译 Registry 继续只迁移经过核验且原文、字段用途、格式兼容的译文。新卡与未知文本保留英文。
- 不透明 UI/基础字体资源按精确 client/build/data/manifests 登记独立审核的官方底稿；回滚文件不能充当官方底稿。
- `tools/chinese-maintainer/capture_base.py` 只做本地 Steam depot/CodeResources 交叉证据抓取。它不是厂商签名认证，也不自动批准登记。
- 构建端依赖既有 Python/UnityPy 环境和本地译文资料；这些不随客户端分发。需要在新维护者机器上另行接入自己的合法译文和官方资源。
- 维护器 `update` 只生成候选；订阅客户端 `update` 才是应用已发布资源，两者职责不同。

### 编译和检查

```sh
./tools/subscriber/build.sh
swiftc -D TESTING -swift-version 5 -O tools/subscriber/Client.swift -o /tmp/mtga-cn-test -lz
MTGA_SUBSCRIBER_TEST_BINARY=/tmp/mtga-cn-test python3 -m unittest discover -s tools/subscriber -p 'tests*.py'
```

发布二进制不能带 `TESTING`。测试专用模拟退出游戏、崩溃、ENOSPC、同步故障开关只存在于测试构建。

### 签名密钥

用生产二进制 `keygen --output <仓库外私钥路径>` 生成 Ed25519 私钥；命令只输出对应公钥。私钥权限 0600，另做安全备份，不放进源码、安装包、日志或 Release。丢失私钥需要通过新的安装包明确更换信任，不允许从旧订阅源静默替换公钥。

### 本地打包

```sh
python3 tools/subscriber/publisher.py \
  --run <已完成候选的目录> --output <新的发布暂存目录> \
  --client dist/subscriber/bin/mtga-cn --key <仓库外私钥> \
  --public-key <base64公钥> --revision 1 --channel testing
```

发布器验证实际官方底稿、候选和构建代码，冻结其哈希，生成数据差分后独立重建输出，再复核源文件。任何变化都会停止，未完成时不会生成可用签名索引。差分文件名包含输入/输出身份哈希。每个 release id 只能使用一次；同一游戏的新修订递增 revision。索引 sequence 默认当前 UTC 毫秒数，也可显式传入一个大于该渠道历史值的整数；发布时必须复核它递增。

稳定包需 `--channel stable --acceptance <验收记录>`。记录从 `acceptance-template.json` 开始，只填写真实通过的结果，不能自动把项目改为 true。记录绑定实际官方底稿、候选、版本、构建代码和非目标文件，且需独立工程审查匹配；高风险未知项不能进入 stable。保留测试者原话、范围及实际测试的处理器架构。发布者验收不代表每台订阅者机器均实测通过。

```sh
python3 tools/subscriber/package_installer.py \
  --client dist/subscriber/bin/mtga-cn --profile <发布目录/profile.json> \
  --output <安装包目录>
```

可分发安装包的 profile 必须使用维护者控制的固定 HTTPS 目录。仅离线演示可加 `--local-feed <发布目录>`；安装时解析包内 feed，不把本机个人路径写入可搬运安装包。

### 公开发布与订阅通知

统一主仓库为 `1996Neil/mtga-chinese-updates`：源码、维护工具、监控与分发放在同一公开仓库。已安装客户端的固定 `main/feed`、公钥与历史资源 URL 保持兼容。源码整合不表示新的客户端或汉化资源已经发布；当前客户端仅 ad-hoc 签名，没有 Developer ID、公证或干净下载 Gatekeeper 验收，不修改或重签游戏，不关闭系统安全检查。

`publish_github.sh` 根据完整游戏版本、Steam build 和修订号生成 `mtga-<client_version>-<steam_build>-r<revision>`。同一游戏版本的新修订号必须递增，而且每个新修订独立 Release；不能向旧 r1/r2 共享 tag 增加新修订来替代新版本通知。

发布器先冻结并验签本地内容，然后创建 **draft Release**、上传不可变差分并核对草稿资源（此时需要发布者权限，不声称匿名可访问）；上传通过后才 CAS 推进并读回签名索引，最后发布草稿，使订阅者收到可用版本通知，再核对匿名下载入口。testing 保持 prerelease，不能为发邮件冒充稳定验收。任何上传、索引推送或读回失败都不会提前公开新 Release。若索引已推进而最终发布失败，重试同一个冻结包继续该草稿，不要另造修订或改清单。

现有 r1/r2 共享 tag 只允许使用完全相同的已签名引用续期、验证原有附件；不修改历史清单、不删除旧资源、不向已公开历史 tag 新增附件。索引续期不新建 Release，因此不制造无内容变更的版本邮件。索引30天有效，过期会停止新安装，不破坏已装版本；发布器会保留并验签其他历史游戏版本的引用。

用户安装客户端后可主动检查更新，无需 GitHub 账号。想收邮件时登录免费 GitHub 账号，`Watch → Custom → Releases` 并开启 Watching 的邮件接收；Star 用于收藏和支持，不等于订阅通知。资源发布说明包含「退出游戏 → 检查更新 → 下载并检查 → 安装或更新汉化」操作。GitHub 通知投递取决于用户设置和平台，不保证每封邮件实时送达。

不要在 profile 填 GitHub token、私钥或带凭据的 URL。公开资源更新不需要用户凭据；私有云接收实验代码不是普通安装流程，也不应把旧私有仓库凭据交给用户。尚未实现客户端自更新、后台定时检查、网络镜像切换、密钥轮换协议和多个游戏实例管理。

## 恢复与持久化

有官方底稿时保存 `original-*`，每次操作保存实际改动前 `before-*` 和重建候选 `after-*`。所有候选校验通过、备份完成后，先持久化 journal，再逐文件原子替换；每次提交前再次检查版本、受保护文件、游戏是否停止、资源是否属于允许的前后状态。目录同步错误会停止后续写入。程序异常时自动尝试恢复；强制退出后保留 journal，由 `recover` 恢复。只承诺经测试的进程中断恢复，不宣称已实测断电或所有文件系统故障。

当前仅保留最近一次可操作事务指针，旧事务目录保留作诊断；本版没有自动清理备份，长期使用需关注磁盘空间。原始基底丢失时仍可用 actual-before 恢复，下一次升级可使用发布者提供的签名完整输出；差分重建仍需要完好的独立官方底稿。索引历史和事务不自动跨信任源迁移。

## 依赖与数据边界

客户端：Apple 系统 Foundation、CryptoKit、Darwin、zlib；用 Xcode Swift 编译，无新增第三方包。发布器/测试：Python 标准库。差分及 Steam protobuf 解析器为本项目实现；Steam 格式字段参考 SteamRE/SteamKit 定义。原有构建器依赖仍由 `tools/chinese-maintainer/requirements.lock` 固定。

客户端只读取游戏版本和必要文件哈希，不读取或上传账户、收藏、牌组、Player.log。联网只下载签名静态数据，不执行远端脚本。只允许 card、client、resources、bootstrap_fonts、font_bundle 五层；官方 manifests、可执行文件和动态库保持受保护。社区译文、游戏数据和字体的权利与源代码许可独立，差分不是再分发授权。


## 2026-10-17 私有修复候选（未发布）

同一客户端版本与 Steam build 也可能刷新活动文本库。检查时先检查活动目录及资源名称，再核对签名清单；若资源世代改变，明确等待匹配发布，不再用“找不到旧文件”描述已发生的刷新。不会把旧文件改名覆盖。

若当前某层字节已精确等于本次签名发布的目标输出，可保留该层参与事务。没有官方重建底稿时明确记录 original_available=false，不把现有中文资源或回滚备份标为官方底稿；恢复仍回到实际改前字节，未来使用签名完整资源升级，若发布未提供完整资源且缓存也不可用则停止。

历史私有候选保留既有快捷链接且不覆盖其他同名文件；当前公开安装器会原子替换同名桌面入口（包括旧或失效链接），不跟随链接写入原目标；同名路径为真实文件夹时保留它并报告备用入口。安装完成优先定位实际入口，而不是只打开资源库目录。目录权限或名称冲突会显示备用路径。真实 Finder 和干净下载体验尚未验收。

当前无 Developer ID 证书/Apple Developer Program 会员。本候选仍仅 ad-hoc 签名，没有 Apple 公证，不能声称消除了 Gatekeeper 首次警告。不得移除隔离属性、关闭系统检查或把反复“仍要打开”作为正常发布验收。新客户端及资源候选均不自动发布或安装。
