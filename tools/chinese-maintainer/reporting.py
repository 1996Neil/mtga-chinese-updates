"""Two independent capability results; static checks never imply client PASS."""
from pathlib import Path
from common import save_json, atomic_bytes, read_json, STATE, fingerprint
from user_messages import label

RUNTIME_CHECKS = ('chinese_card_names', 'chinese_rules', 'types_subtypes', 'reminder', 'chinese_UI',
                  'FRA_WPN', 'font_rendering', 'english_search', 'chinese_search', 'Mountain_Opt_Hearth',
                  'linked_faces', 'adventure', 'basic_lands', 'reprints', 'tokens_excluded', 'restart_repeat')


def write_report(run_dir, report):
    loc, imp = report['localization'], report['import_compatibility']
    lines = [f'# MTGA {report["client_version"]} — 中文汉化与双语导入维护报告', '',
             '阅读顺序：先看汉化与导入的独立结果，再看改动数字、安装门槛及验收步骤。', '',
             f'状态：{label(report["state"])}。候选生成不等于客户端验收，也不等于已安装。', '',
             '| 能力 | 静态检查 | 当前候选游戏内验收 |', '| --- | --- | --- |',
             f'| 中文汉化 | {label(loc["static_status"])} | {label(loc["client_status"])} |',
             f'| 双语牌组导入 | {label(imp["static_status"])} | {label(imp["client_status"])} |', '',
             f'**是否允许正式安装：{"是" if report["safe_to_install"] else "否"}**', '',
             '## 更新实际破坏了什么', '',
             f'官方资源中主正面非 token 的 {report["observed"]["primary_titles"]:,} 个 TitleId，中文 F1 数量为 {report["observed"]["chinese_primary_F1"]:,}。',
             '旧汉化和旧补齐的英文 F0 已被官方资源替换；官方英文 F1 本身仍可供英文导入。',
             f'当前官方资源下，历史英文 60 张原生导入：{report.get("official_english_baseline", "未验证")}。此结果只适用于原版基线。', '',
             '## 中文显示', '',
             f'- 卡库 F1 安全恢复：{loc["card"]["counts"]["applied"]:,} 条（不是卡牌张数）。',
             f'- 主正面中文卡名 F1：{imp["names"]["chinese_F1"]:,} / {imp["names"]["primary_title_ids"]:,}。英文 F0 不计入漏译。',
             f'- ClientLocalization 安全恢复：{loc["client"]["counts"]["applied"]:,} 个 enUS 字段。',
             f'- resources.assets UI 恢复：{loc["resources"]["changed_UI_entries"]:,} 个英文语言槽。',
             f'- 已迁移 FRA 卡库字段：{loc["card"]["counts"]["by_source"].get("FRA-card", 0):,}；FRA 客户端 Key：{loc["client"]["counts"]["by_source"].get("FRA-client", 0):,}；WPN Key：{loc["client"]["counts"]["by_source"].get("WPN-client", 0):,}。',
             f'- 字体检查：{label(loc["fonts"].get("status", "FAIL"))}；游戏内实际显示：{label(loc["fonts"].get("runtime_rendering", "未验证"))}。', '',
             '新 Key、原文变化、字段用途未知、来源冲突、格式不安全的记录留在 registry；无可靠译文保持新版英文。',
             '保留历史英文赛制/活动名称。donor 差异保存在 decisions 的 conflicts 中，不覆盖优先的已验证译文。', '',
             '各层具体计数与失败原因见 report.json、card/client/ui-decisions.json。needs-review 与 keep-english 的分母不是卡牌数量。', '',
             '## 双语牌组导入', '',
             f'- 从新版官方数据生成缺失英文 F0：{imp["names"]["generated_F0"]:,}。',
             f'- 已有官方 F0 保留：{imp["names"]["existing_F0_preserved"]:,}，包括带重音的名称。',
             f'- 新增可导入卡牌：{imp["fixtures"]["new_importable_cards"]:,}。', '',
             '| 测试 | 静态身份/数量匹配 | 原生 MTGA 客户端 |', '| --- | --- | --- |']
    for name, value in imp['fixtures']['fixtures'].items():
        lines.append(f'| {label(name)}（{name}） | {label(value["static_status"])} | {label(value["native_client_status"])} |')
    lines += ['', '双面/冒险/重印/基本地/token 的实际结构、关联及排除规则已纳入验证。共享 TitleId 的 token/副面影响单独报告；不向 token-only 或副面-only 名称添加 F0。',
              f'特殊结构游戏内验收：{label(imp["fixtures"]["special_client_status"])}。数据库匹配不能证明游戏内导入通过。', '',
              '## 安装与回滚', '',
              f'阻塞项：{"；".join(report["blockers"]) or "无"}。', '',
              '下一步：先处理上面的阻塞项。临时测试、验收记录及恢复说明见菜单 6，或执行 `mtga-cn guide`。', '',
              'update 只构建与校验候选。install 要求两项能力及当前候选游戏内验收都通过，并且 MTGA 已退出。',
              '首次候选需要在受控 test-session 中取得客户端证据；该临时测试会保存所有本次改动前文件与官方 manifest，失败可精确回滚。',
              'rollback 恢复最近一次安装/测试之前的实际文件状态；不回到固定旧游戏版本。后续官方更新或未知文件变化时拒绝覆盖。', '',
              '## 当前证据与限制', '',
              '- 新版官方 Raw 的大小与 SHA256 对齐官方 manifest。',
              '- 每层重新读取候选，逐项核对非目标表、行、字段、对象与依赖。',
              '- 官方 manifest、可执行文件、GameAssembly、账户/收藏/现有牌组未修改。',
              '- 字体只更新中文 fallback 与源字体；CRC 相等不等于原文件 SHA 相等，真实加载仍需要验收。',
              '- 后续更新沿用同一命令；未知 schema、资源布局或高风险变化会停止安装，不能承诺未来任何版本无需适配。', '']
    if report.get('source_audit'):
        a = report['source_audit']
        lines += ['## 译文来源与官方规则文本审计', '',
                  f"待审记录：{a['pending_total']}；已关联来源检查：{len(a['registered_evaluations'])}；语义审查记录：{a['semantic_review_records']}。",
                  '详情见 source-audit/REPORT.md 和 report.json。自动来源检查不代表人工批准。', '']
    save_json(run_dir / 'report.json', report)
    atomic_bytes(run_dir / 'REPORT.md', '\n'.join(lines).encode())


def code_fingerprints():
    from common import ROOT, fingerprint
    result = {p.name: fingerprint(p) for p in sorted(ROOT.glob('*.py'))}
    for path in (ROOT / 'requirements.lock', ROOT / 'source-format-reviewed-pairs.json', ROOT.parents[1] / 'bin/mtga-cn'):
        result[str(path)] = fingerprint(path)
    return result


def runtime_for(run_dir, report):
    path = run_dir / 'runtime-verification.json'
    if not path.exists():
        return None
    evidence = read_json(path)
    if evidence.get('candidate_files') != report['candidate_files']:
        return None
    if (evidence.get('evidence_kind') != 'direct_user_confirmation' or not evidence.get('user_quote') or
        evidence.get('localization_status') != 'PASS' or evidence.get('import_status') != 'PASS' or
        not all(evidence.get('checks', {}).get(k) is True for k in RUNTIME_CHECKS)):
        return None
    for kind in report['import_compatibility']['fixtures']['fixtures']:
        row = evidence.get('exports', {}).get(kind, {})
        if evidence.get('imports', {}).get(kind) != 'PASS' or row.get('TitleIds_and_60_quantity_verified') is not True:
            return None
        try:
            if fingerprint(Path(row['path'])) != {k: row[k] for k in ('sha256', 'bytes')}:
                return None
        except (KeyError, OSError, RuntimeError):
            return None
    return evidence
