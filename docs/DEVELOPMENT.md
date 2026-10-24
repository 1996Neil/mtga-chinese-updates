# 自行构建、测试与维护

普通用户从 Releases 下载客户端，见根 README。本页面向愿意 Fork、自行构建或维护适配的开发者。

## 获取源码与编译客户端

在 macOS 安装 Xcode Command Line Tools（`xcode-select --install`），并准备 Python 3.11。维护器当前锁定的 UnityPy 1.23.0 在 Python 3.14 上不能构建；CI 显式使用 3.11，不能跟随系统 Python 自动升级。客户端是 Swift 原生程序，发布流程所需的 GitHub CLI 由维护者自行安装与登录。

```sh
git clone https://github.com/1996Neil/mtga-chinese-updates.git
cd mtga-chinese-updates
zsh tools/subscriber/build.sh /tmp/mtga-client-build
xcrun swiftc -D TESTING -swift-version 5 -O tools/subscriber/Client.swift -o /tmp/mtga-client-tests -lz
MTGA_SUBSCRIBER_TEST_BINARY=/tmp/mtga-client-tests python3 -m unittest discover -s tools/subscriber -p 'tests*.py' -v
```

`build.sh` 编译 arm64 与 x86_64 并组合为 universal 程序，只做本地 ad-hoc 签名；不代表 Developer ID、公证、新电脑下载或真实游戏验收通过。测试二进制只用于合成夹具，不能作为正式交付程序。

已有客户端继续使用原 `main/feed` 与固定公钥，普通用户无需更换仓库地址。自己 Fork 后若要运营独立更新源，须创建自己的签名密钥、配置和发布仓库地址，不会自动获得原发布者私钥。不要将私钥、Token 或 `.env` 提交到任何 Git 仓库。

## 维护器环境

```sh
python3.11 -m venv tools/chinese-maintainer/.venv
tools/chinese-maintainer/.venv/bin/python -m pip install -r tools/chinese-maintainer/requirements.lock
cd tools/chinese-maintainer
.venv/bin/python -m unittest discover -v
```

通用测试在临时目录中运行。真实底稿测试默认明确跳过；只有已经有相应合法本地资料时才启用：

```sh
MTGA_RUN_LOCAL_INTEGRATION=1 .venv/bin/python -m unittest tests_base.PinChainTests -v
```

启用后缺少真实底稿、收据或哈希不符会失败，不能将其伪造为通过。CI 不携带游戏文件，不跑真实 MTGA。

从任意检出位置可执行 `./bin/mtga-cn guide`，或打开 `bin/MTGA 汉化与双语导入维护.command`。源码启动器按自身位置找虚拟环境，不使用维护者电脑的固定安装路径。

默认游戏路径位于当前用户的 Steam 库。可在启动前设置绝对路径：

```sh
export MTGA_GAME_PATH="/你的Steam库/steamapps/common/MTGA"
export MTGA_BACKUP_ROOT="/你的本地备份目录"
export MTGA_HISTORY_ROOT="/你的已核验历史导入资料目录"
```

备份默认位于当前用户 `Library/Application Support/MTGAChineseMaintainer/backups`。维护状态默认在 `tools/chinese-maintainer/state/`；它和审译包、外部数据不会随 Git 同步。

## 为什么只拉源码还不能立即生成完整汉化

候选构建需要与当前游戏匹配的官方底稿、经过核验的历史中文/来源、牌面身份和用途映射、字体及验收材料。源文件不能替代这些资料，也不能把回滚备份冒充官方底稿。

目前 Registry 的首次导入仍包含历史资料结构（`registry.bootstrap`）；自己维护时须准备自己的合法历史来源及对应结构，或开发新的显式导入器并为其建立测试。没有这些资料时可编译客户端、跑通用测试、检查协议和贡献代码，但不能声称一次命令就能重建作者的整库汉化。原机器的固定底稿 pin 也不是新机器通用验收凭证。修改 `capture_base.py` 的 pin 之前必须完成它所要求的独立来源核验。

新补译按 [来源查询规范](../tools/chinese-maintainer/SOURCE_LOOKUP.md) 查找并保存逐项证据。`source_check.py export` 导出核对队列，`validate` 校验 V2 记录与原始快照。官方、社区及 AI 来源分开。原有1,837项本地来源包没有公开整库上传；不要将源码合并当作发布这批候选。

历史证据中的旧绝对路径有专门兼容映射；保留其原始内容与哈希，不批量替换。对新资料使用当前路径和明确证据。

## 发布

维护者先构建、审查和完成所需游戏验收，再按 `tools/subscriber/README.md` 生成签名差分并发布。新版采用每修订独立 tag；资源先上传核验、索引成功推进后才发布 Release 通知。只续签旧索引不代表新汉化发布。

普通测试不会调用真实发布、安装游戏或发送通知。`testing` 不能冒充 `stable`。发布测试夹具没有用到真实私钥。

## 监控与旧原型

`mtga-watch/` 是当前公开维护监控。查看 Actions 的 MTGA update watch，失败有报告；官方公告或已知清单变化不代表汉化可安装。独立 Localization 全局最新指针仍未核实，限制保留在报告中。

`tools/subscriber/private_*`、`install_private_receiver.py` 与 `PRIVATE-DELIVERY-20261017.md` 保留了旧私有云端固定 r3 原型的源码和证据范围。它们不在普通用户入口使用，不改为读取公开仓库以绕过私有守卫，也不承诺旧制品在到期后可用。相关旧仓库归档后不再生成私有制品；新维护工作使用公开监控、当前本地构建与签名 feed 发布。旧接收器的本地恢复边界不因归档被改写。
