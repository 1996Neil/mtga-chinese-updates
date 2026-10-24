# card-name-map —— MTGA 卡表中英对照（离线速查）

用一个地方存「英文牌名 ↔ 官方中文牌名」，方便 AI 读 MTGA / 17Lands 日志时直接把英文牌名映射回中文再给建议。

## 当前快速轮抽（2026-10-21）

| | 系列 | 中文 | 时间窗 |
|---|---|---|---|
| **现在** | Aetherdrift（DFT） | **乙太飘移** | 2026-10-19 ~ 10-26 |
| 下周 | Reality Fracture（FRA） | 现实裂界 | 2026-10-27 ~ 11-06 |

两个系列的卡表都已生成好：`sets/DFT/`、`sets/FRA/`。状态写死在 `current-quickdraft.json`，每周换系列时改它 + 跑一次 `build_set.py`。

> 日程来自 [draftsim 的 Arena 日程表](https://draftsim.com/mtg-arena-schedule/)（2026-10-21 读取）。官方 announcements 的日程链接当天 404，未能直接核对，牌池以游戏内为准。

## 中文名从哪来

**就是你自己那套汉化流程的数据**（`mtga-cn`）：

```
tools/chinese-maintainer/state/registry.sqlite  →  card_names 表
    english_canonical / chinese_display
```

它记录的是 **MTGA 官方客户端本地化文本**（不是社区翻译），所以：
- 与游戏内汉化后的显示名**完全一致**（同一份字符串）；
- 不需要重新翻译、不需要对图，直接复用，零新增误差。

**这是唯一可行的简中来源。** 我实测过两个常见数据源：
- Scryfall：DFT 的 `lang:zhs` 返回 0 张（不收中文卡）
- MTGJSON：DFT 的 `foreignData` 只有法/德/意/日/西，**没有 Chinese**
原因是中国区实体卡简中印刷早已停，所以实体卡数据库里根本没有中文名，只有 Arena 客户端还保留简中。你的这套 registry 因此不是"能不能复用"的问题，而是**绕不开**。

元数据（稀有度 / 颜色 / 法术力费用 / 类型）来自 Scryfall API，用 **Arena GrpId = Scryfall `arena_id`** 精确对齐，避免同名卡（如基本地）错配。

## 用法

```bash
# 1) 生成某个系列的卡表（写到 sets/<CODE>/）
cd ~/Documents/ChatGPT/mtga/tools/card-name-map
python3 build_set.py DFT

# 2) 查牌名（英文 -> 中文，也认中文 -> 英文）
mtga-cardmap "Canyon Vaulter" "Broadcast Rambler"
# Canyon Vaulter = 峡谷飞跃手｜非普通 ｜白 ｜1W ｜Creature — Kor Pilot

# 3) 日志里的 GrpId 直接贴（DraftPack 数组就是一堆 GrpId）
mtga-cardmap --grp "94803, 94804, 94805"

# 4) 一整段日志/牌池文本，自动抽出其中所有牌名
mtga-cardmap --scan < pack.txt

# 5) 列某个稀有度
mtga-cardmap --list mythic
```

`mtga-cardmap` 已软链到 `~/.local/bin`。默认系列读 `current-quickdraft.json` 的 `current.set_code`，所以每周换系列只改这一个文件就够；查别的系列加 `--set FRA`。

## 产物

每个系列输出四份，都在 `sets/<CODE>/`：

| 文件 | 用途 |
|---|---|
| `<CODE>-cards.html` | 可直接打开的单文件检索页（搜索 + 稀有度筛选），给别人看最好用 |
| `<CODE>-cards.md` | 人看 / AI 读的速查表，按「颜色 · 稀有度」分组的 Markdown 表格 |
| `<CODE>-cards.csv` | 表格工具、脚本处理（UTF-8，Excel 直接开） |
| `<CODE>-cards.json` | 程序/AI 结构化读取，含 arena_id、scryfall_id、`zh_verified` |

## 口径

- 只把 **IsPrimaryCard=1** 的牌当作主系列；异画 / 展示框 / 无边框等重复版本标 `is_primary=0`，留在 CSV/JSON 里但不出现在 md 表格中。
- 衍生物（IsToken=1）单独标记，不混进牌池。
- 中文缺失时**保留英文并标 `zh_verified=false`**，绝不机翻填充。
- 只读：不写入 `registry.sqlite`，不碰 MTGA 安装目录。

## 每周换系列（三步）

```bash
cd ~/Documents/ChatGPT/mtga/tools/card-name-map
python3 build_set.py FRA          # 1. 生成新系列卡表（DFT/FRA/DSK/EOE… 都已在 registry 里）
# 2. 改 current-quickdraft.json 的 current / next
mtga-cardmap "某张牌"             # 3. 默认系列自动切过去
```

先查 registry 里有哪些系列代码：

```bash
sqlite3 "file:$HOME/Documents/ChatGPT/mtga/tools/chinese-maintainer/state/registry.sqlite?mode=ro&immutable=1" \
  "select json_extract(identity_json,'\$.ExpansionCode') c, count(*) from card_names group by c order by 2 desc limit 30;"
```

`registry.sqlite` 常被 `mtga-cn` 写入（会留 `-journal`）。脚本已做处理：先试普通只读，被锁就自动退到 `immutable=1`，不会阻塞你的汉化流程。
