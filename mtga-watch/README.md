# MTGA 更新监控与本地维护

统一仓库：`1996Neil/mtga-chinese-updates`。监控每6小时检查官方状态页、Steam macOS depot及已观测的资源清单，并读取普通用户实际使用的签名订阅索引。它不下载游戏资源、不登录玩家账号、不构建或安装汉化，也不自动发布用户 Release。

## 两类通知不要混淆

**维护者通知**保留两阶段：

1. 公告／维护预告：包含官方发布时间、计划维护的北京时间、预计时长及距结束的小时数。官方没有结束时间就不估算；公告不代表相关资源已变化。
2. 后续行动提醒：官方转为 completed/resolved 时说明可检查官方下载；Steam build、macOS depot或资源身份变化时说明可开始对比和适配。两者都不代表汉化已经可用。

配置 `maintainer_notifications: "issues"` 且显式使用 `--notify-maintainer` 时，只有**未通知的新事件**才在当前仓库建 issue 并 @仓库所有者；GitHub按维护者的个人设置处理邮件，建单不等于邮箱实收。schedule使用此显式开关，保留此前维护者已授权的两阶段通知。不会复制旧私有issue正文或用户资料。

**普通用户只订阅 Releases**，在资源构建、校验及验收完成后收到“可更新”通知。不要让普通用户 Watch 全部仓库活动。监控issue不是可安装通知，不能据此提前发布或更新。

## 默认检查与查看位置

- `python3 mtga-watch/watch_mtga.py --dry-run`：读取本地迁移状态，查询真实公共来源，只写指定本地报告；不调用 GitHub 通知或状态写接口。
- `--with-steam`：额外在Linux环境匿名运行SteamCMD，检查public build和macOS depot，需32位运行库。
- 无 `--dry-run` 时从GitHub main读取固定 `mtga-watch/state.json`，用Contents API带旧blob SHA的单文件乐观比较交换保存状态。没有显式通知开关的手动检查仅保存看板，不发消息。
- 维护者可在 Actions → MTGA update watch → 对应run summary 查看报告；公开 `mtga-watch/state.json` 保存最近检查状态、公开来源事件和通知去重键。
- 手动 Actions 默认 `dry_run:true`、`notify_maintainer:false`。初次切换验证必须保留这两个默认值。

这里的 `seen_keys` 指**已通知或从旧通知迁入的事件键**，不是每次看板观察的全集。手动只看报告不消耗通知资格；发单失败也不确认事件，之后可重试。若建单成功但状态提交失败，下一轮会先检查当前仓库全部状态issue的marker，避免重复发单。公开仓库中只信任仓库所有者与 `github-actions[bot]` 的事件issue，访客伪造marker不能吞掉维护通知。CAS冲突会失败退出，不强制重写。

`state.json`有严格schema、数量和键格式检查，缺失或异常时失败，不能悄悄重置为空状态。报告保留异常，不会把来源读取失败写成“没有更新”。Actions只需标准 `contents:write` 和 `issues:write`，不使用旧仓库的云端试装密钥或玩家凭据。

## 从旧监控仓库切换

旧仓库是 `1996Neil/mtga-resource-acquisition-probe`。一次性完整导出 `issues?state=all` 全部分页（含已关闭），留在私有本地审计目录，然后执行：

```sh
python3 mtga-watch/migrate_watch_state.py \
  --issues /私有本地路径/旧事件.json \
  --output /新文件路径/state.json \
  --confirm-complete-history
```

迁移只提取 `<!-- mtga-watch:稳定事件键 -->` 与更早的Steam build标题中的数字；不复制旧正文、issue号码、用户名、账号信息或任何凭据。输出为独占新文件，附源文件SHA256供本地核验，绝不通过迁移脚本发消息。

安全切换顺序：

1. 核实旧仓最新提交与完整issue导出，生成并审核新state。
2. 停用旧仓MTGA update watch，等待正在运行的旧任务结束；重新导出旧任务可能产生的新issue并复核迁移键。
3. 将代码、配置及最终state提交到新仓main，保留原feed全部字节、资源地址和公钥。
4. 手动运行新仓 `dry_run:true`、`notify_maintainer:false`，检查来源、去重、权限和日志不含私人信息。该验证不新建issue、不推进state、不通知用户。
5. 验证通过后保留新仓schedule，确认旧schedule停用，再给旧仓迁移说明并归档。

新repo没有历史issue，不能仅改REPO名称后直接启动通知。状态迁移前也不能把旧公告重新建单。首次dry-run只是检查：不把新发现的变化凭空认定为过去已通知。

## 资源覆盖与限制

- Steam匿名app_info：public build和macOS depot2141913的manifest GID。
- 官方状态页近期更新公告、维护窗口及近期完成状态；普通登录故障不会冒充新版。
- `main/feed/index-<channel>.json`：使用与客户端相同的公钥，验签索引和清单，核对大小与SHA256；当前 channel 由 `config.json` 指定，数字版本/build/revision 最新项才是已交付基准。
- 已观察macOS External指针、主manifest和独立Localization manifest：云端每次重新读取、验证内容地址并比较资源名／长度／SHA256。

尚不能保证发现Doorbell在build和External都不变时下发的全新独立Localization指针。匿名Doorbell曾返回401，未有经验证的匿名全局latest；报告永久保留 `independent_localization_latest=UNVERIFIED_REQUIRES_NEW_OFFICIAL_POINTER`。不要用玩家登录或已存令牌绕过。新发布后旧观测与发布不匹配仅提醒刷新，不把旧语言包误报为热更新。

## 官方更新后的本地维护

1. Steam及游戏内下载完成，退出游戏；核对可信官方底稿和备份。
2. 按 `tools/chinese-maintainer/SOURCE_LOOKUP.md` 核对原文、牌张身份、已有中文与格式；缺少官方译文的专用文案按用户确认偏好明确标注AI表达。
3. 构建五层候选，核对F1中文显示与F0英文导入、UI、字体和非目标数据；完成独立复核及本次必要的游戏内验证。
4. 通过发布器签名资源，先上传校验，再推进签名索引；索引可用后才发布独立Release通知普通用户。
5. 所有人使用同一个订阅客户端检查→预演→退出游戏→应用，保存备份；监控并不自动执行这些步骤。

更新种子可运行 `export_local_resource_watch.py --output /新路径/resource-seed.json`。它只提取公开CDN文件名和版本/清单身份，不上传Player.log、账户、令牌或游戏文件；种子仍是本机实际观测，不是全局最新承诺。

## 验证

```sh
python3 -m unittest discover -s mtga-watch -p 'test_*.py' -v
python3 mtga-watch/watch_mtga.py --dry-run
```

Python标准库和系统OpenSSL验签，无新增Python依赖。SteamCMD从Valve官方HTTPS引导下载；下载摘要不冒充独立签名认证。合成测试不证明实际新邮件到达，也不证明新游戏版本已验收。
