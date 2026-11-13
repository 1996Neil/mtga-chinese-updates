# 查牌映射规则

牌名优先从本机已生成的 `sets/` 卡表查询，不凭记忆猜译。完整汉化来源规则见 [来源核对规范](../chinese-maintainer/SOURCE_LOOKUP.md)。

- 先 `python3 fetch_names.py --check`；缓存缺失时 `--apply` 拉取 pinned `mtga-card-names` release。不要直接读 `registry.sqlite`。
- `mtga-cardmap --grp "94803,94804"` 按 Arena GrpId 查；名称查询直接传英文名；`--set FRA` 显式选系列。
- 卡表由 `build_set.py` 从公开 names-only release 取中文名，从 Scryfall 取元数据（按 `arena_id` 精确对齐）；不是官方中文声明，也不随源码提供完整卡库。
- 查不到时先确认系列、牌面、再平衡和 GrpId，然后按来源规范查大学院废墟、Scryfall 对应牌面及大陆官方公告。保留出处，不能声称这些站点一定无中文。
- `zh_verified` 只表示有中文且非英文回退，不表示官方译文或人工验收；来源可能是官方、社区或已标注的 AI。
- 费用、颜色、稀有度等按精确身份匹配；未确认的字段不能猜造。
