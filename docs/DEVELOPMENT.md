# 自行构建、测试与维护

普通用户从 Releases 下载客户端，见根 README。本页面向愿意 Fork、自行构建或维护适配的开发者。

维护政策、文本修正与仓库来源统一见 [维护规则与证据](MAINTENANCE.md)，日常操作不再按日期文档选择入口。监控和下载验证跟随当前发布渠道；完整社区基线只在独立私有库保存。

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

备份默认位于当前用户 `Library/Application Support/MTGAChineseMaintainer/backups`。维护状态默认在 `tools/chinese-maintainer/state/`；真实Registry、原始审查包及外部数据库不会随Git同步。筛选后的可复用译文和来源索引位于 [`maintenance-data/`](../maintenance-data/README.md)，不附带原始证据包。

## 为什么只拉源码还不能立即生成完整汉化

候选构建需要与当前游戏匹配的官方底稿、经过核验的历史中文/来源、牌面身份和用途映射、字体及验收材料。源文件不能替代这些资料，也不能把回滚备份冒充官方底稿。

目前 Registry 的首次导入仍包含历史资料结构（`registry.bootstrap`）；自己维护时须准备自己的合法历史来源及对应结构，或开发新的显式导入器并为其建立测试。没有这些资料时可编译客户端、跑通用测试、检查协议和贡献代码，但不能声称一次命令就能重建作者的整库汉化。原机器的固定底稿 pin 也不是新机器通用验收凭证。修改 `capture_base.py` 的 pin 之前必须完成它所要求的独立来源核验。

新补译按 [来源查询规范](../tools/chinese-maintainer/SOURCE_LOOKUP.md) 查找并保存逐项证据。`source_check.py export` 导出核对队列，`validate` 校验 V2 记录与原始快照。官方、社区及AI来源分开。原有1,837项本地审查包和后续13条修正的精简投影已公开到 `maintenance-data/2026.64.50/`；原始审查包、快照和数据库仍未整库上传。公开投影不替代当前官方底稿和准入，不将审查状态升级为人工或游戏验收，也不表示新的玩家资源已经发布。

历史证据中的旧绝对路径有专门兼容映射；保留其原始内容与哈希，不批量替换。对新资料使用当前路径和明确证据。

## 发布

维护者先构建、审查和完成所需游戏验收，再按 `tools/subscriber/README.md` 生成签名差分并发布。新版采用每修订独立 tag；资源先上传核验、索引成功推进后才发布 Release 通知。只续签旧索引不代表新汉化发布。

普通测试不会调用真实发布、安装游戏或发送通知。`testing` 不能冒充 `stable`。发布测试夹具没有用到真实私钥。

## 监控与旧原型

`mtga-watch/` 是当前公开维护监控。查看 Actions 的 MTGA update watch，失败有报告；官方公告或已知清单变化不代表汉化可安装。独立 Localization 全局最新指针仍未核实，限制保留在报告中。

监控在 Ubuntu 24.04 + OpenSSL 运行，CI 在同一平台测试其 Ed25519 签名夹具。macOS 自带 LibreSSL 不支持这里使用的 `pkeyutl -rawin`；若本机测试监控，须先准备支持 Ed25519 的 OpenSSL 并放在 PATH，不得关闭验签来适配系统工具。

旧私有云端固定 r3 原型及其过程产物已移除，可从 Git 历史恢复。新维护工作使用公开监控、当前本地构建与签名 feed 发布。

`mtga-watch/export_local_resource_watch.py` 仍用于游戏更新后生成公开资源监控种子；`migrate_watch_state.py` 保留旧通知去重状态的迁移工具及回归测试。定时任务不直接调用它们，不表示维护者不再需要它们。`compatibility/` 保留历史导入样本及原有路径映射。

## 私有社区基线与工作量报告

完整基线存放在独立私有库 `1996Neil/mtga-chinese-baseline`。公开源码不包含整库文本，也不依赖审查任务仓库。未配置基线时沿用原有流程；显式配置后文件缺失或哈希变化会报错。

```sh
export MTGA_BASELINE_ROOT="/你的检出位置/mtga-chinese-baseline"
# 默认取当前候选版本；显式复用旧基线仍需逐项匹配当前输入：
# export MTGA_BASELINE_VERSION="2026.64.50"
./bin/mtga-cn update
./bin/mtga-cn report --run /当前候选目录 --since-run /上一版候选目录 \
  --registry tools/chinese-maintainer/state/registry.sqlite
```

`report` 仅在内存中复演已有准入，不联网、不修改 registry、不生成候选。未提供 `--registry` 时只评估基线；未提供 `--since-run` 时新增和英文变化显示为未知，不能把“基线未覆盖”称为“本版新增”。需处理项大于零时退出码为 1。

基线 manifest 绑定原始 JSONL、官方 Raw 输入指纹及逐槽位英文/身份/用途哈希。基线只对符合绑定、包含中文且格式安全的条目补位；已审记录、明确保留英文、来源冲突及不适用的已审译文继续沿用原有检查。已有来源及译文不删除、不改标。卡牌和客户端分开判断，客户端基线不借给 resources UI 槽位。

生成基线需要社区中文输入和同版已捕获官方底稿；仅有英文游戏不能生成社区中文。使用新输出目录，避免覆盖现有版本：

```sh
tools/chinese-maintainer/.venv/bin/python tools/chinese-maintainer/build_baseline.py \
  --version 2026.64.50 --official-run /已捕获官方底稿目录 \
  --community-client /社区中文客户端数据库.mtga \
  --community-card /社区中文卡牌数据库.mtga \
  --output /新的私有版本目录
```

历史 JSONL 可用 `--client-jsonl` 与 `--cards-jsonl` 原字节导入，替代两个 community 参数。生成器不改变中文，不能把社区译文标为官方或已完成人工/游戏验收。

直接提取社区数据库时，生成器同时核对社区英文、牌张身份和字段用途与官方是否一致。历史 JSONL 不含原社区库的身份信息，导入只能绑定现有英文到当前官方上下文；manifest 的 `binding_basis` 会披露这个限制，不能据此声称重新核验过原社区身份或语义。

## 发布编排

```sh
./bin/mtga-cn release --version 2026.64.50 --run /候选目录 --dry-run \
  --revision 4 --client /已编译客户端 --key /本地私钥 \
  --public-key-file /公钥文件
```

省略 `--run` 时先执行既有 `update` 构建；dry-run 随后重验候选并打印计划，不读取私钥、不签名、不发布。输出中的 `blockers` 必须清零才可继续。移除 `--dry-run` 会调用既有发布器构建并验证签名差分；只有显式添加 `--publish` 才调用远端发布器。`--channel stable` 还必须提供 `--acceptance`，并通过当前候选的工程审查和游戏验收。编排器不会填造这些证据。已有手工命令继续可用。
