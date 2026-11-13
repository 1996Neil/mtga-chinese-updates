# card-name-map —— MTGA 卡表中英对照（离线速查）

用一个地方存「英文牌名 ↔ 简体中文牌名」，方便 AI 读 MTGA / 17Lands 日志时直接把英文牌名映射回中文再给建议。

中文牌名来自公开的 names-only 包：

- 仓库：<https://github.com/1996Neil/mtga-card-names>
- 当前 pin：`RELEASE.json`（release、data_version、manifest/gz/jsonl 三个 sha256、行数）
- 本地缓存：`data/cards.jsonl.gz` + `data/manifest.json`（gitignored）

卡牌元数据（稀有度 / 颜色 / 法术力费用 / 类型）来自 Scryfall API，用 **Arena GrpId = Scryfall `arena_id`** 精确对齐，避免同名卡（如基本地）错配。

## 当前快速轮抽

| 示例 | 系列 | 中文 |
|---|---|---|
| | Aetherdrift（DFT） | 乙太飘移 |
| | Reality Fracture（FRA） | 现实裂界 |

`current-quickdraft.json` 记录当前/下一个系列（含 `checked_at`，以游戏内 Events 为准），`mtga-cardmap` 默认读它；每周换系列只改这个文件。

## 第一次使用

```bash
cd ~/Documents/MTGA-Chinese/tools/card-name-map

# 1) 拉取 pinned names-only release 到 data/（校验 manifest/gz/jsonl 三层 hash）
python3 fetch_names.py --apply
python3 fetch_names.py --check

# 2) 生成系列卡表（联网取 Scryfall 元数据）
python3 build_set.py FRA
```

离线/只要名字：

```bash
python3 build_set.py FRA --offline
```

如果已经手动下载了 release 资产：

```bash
python3 fetch_names.py --input /path/to/cards.jsonl.gz \
  --manifest /path/to/manifest.json --apply
```

## 用法

```bash
# 英文 -> 中文（也认中文 -> 英文）
mtga-cardmap "Canyon Vaulter" "Broadcast Rambler"
# Canyon Vaulter = 峡谷飞跃手｜非普通 ｜白 ｜1W ｜Creature — Kor Pilot

# 日志里的 GrpId 直接贴（DraftPack 数组就是一堆 GrpId）
mtga-cardmap --grp "94803, 94804, 94805"

# 一整段日志/牌池文本，自动抽出其中所有牌名
mtga-cardmap --scan < pack.txt

# 列某个稀有度
mtga-cardmap --list mythic

# 指定系列
mtga-cardmap --set FRA "某张牌"
```

`mtga-cardmap` 已软链到 `~/.local/bin`（或直接运行 `./mtga-cardmap`）。默认系列读 `current-quickdraft.json` 的 `current.set_code`；查别的系列加 `--set FRA`。

## 产物

每个系列输出四份，都在 `sets/<CODE>/`：

| 文件 | 用途 |
|---|---|
| `<CODE>-cards.html` | 可直接打开的单文件检索页（搜索 + 稀有度筛选） |
| `<CODE>-cards.md` | 人看 / AI 读的速查表，按「颜色 · 稀有度」分组的 Markdown 表格 |
| `<CODE>-cards.csv` | 表格工具、脚本处理（UTF-8，Excel 直接开） |
| `<CODE>-cards.json` | 程序/AI 结构化读取，含 arena_id、scryfall_id、`data_version`、`zh_verified` |

`sets/`、`data/`、`current-quickdraft.json` 都是本地生成/缓存，不提交。

## 口径

- 只把 **IsPrimaryCard=1** 的牌当作主系列；异画 / 展示框 / 无边框等重复版本标 `is_primary=0`，留在 CSV/JSON 里但不出现在 md 表格中。
- 衍生物（IsToken=1）单独标记，不混进牌池。
- 中文缺失时**保留英文并标 `zh_verified=false`**，绝不机翻填充。
- `zh_verified` 只表示「有中文且不是英文回退」，**不表示官方译文或人工验收**；来源可能是官方客户端、社区或已标注的 AI。
- 只读：不写 names 缓存、不碰 MTGA 安装目录。

## 每周换系列

```bash
cd ~/Documents/MTGA-Chinese/tools/card-name-map

python3 build_set.py FRA          # 1. 生成新系列卡表
# 2. 改 current-quickdraft.json 的 current / next
mtga-cardmap "某张牌"             # 3. 默认系列自动切过去
```

如果新系列缺中文，先更新 `RELEASE.json` 到新的 `mtga-card-names` release，再：

```bash
python3 fetch_names.py --apply
python3 fetch_names.py --check
```

## 重复副本

`MTGA-Maintenance/tools/card-name-map/` 已删除；唯一实现是公开仓库里的这一份。维护库的 `SOURCE_HANDOFF.md` 已改为指向这里。
