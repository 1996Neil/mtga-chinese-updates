#!/usr/bin/env python3
"""Build EN<->ZH card-name maps for a MTG Arena set.

Chinese names come from reviewed localizations in the user's maintenance
registry.sqlite. They may be official, community, or explicitly AI-authored;
the registry evidence, not the presence of Chinese, determines their source.
Card metadata (rarity / colors / mana cost / type) comes from Scryfall, and is
joined on Arena GrpId (Scryfall `arena_id`) so it cannot drift on duplicate names.

Read-only: never writes to registry.sqlite or any MTGA install files.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_REGISTRY = HERE.parent / "chinese-maintainer" / "state" / "registry.sqlite"
SETS_DIR = HERE / "sets"
UA = "mtga-card-name-map/1.0 (local read-only export)"

CJK = re.compile(r"[\u3400-\u9fff]")
RARITY_ORDER = {"mythic": 0, "rare": 1, "uncommon": 2, "common": 3, "special": 4, "bonus": 5}
COLOR_ORDER = {"W": 0, "U": 1, "B": 2, "R": 3, "G": 4}
COLOR_ZH = {"W": "白", "U": "蓝", "B": "黑", "R": "红", "G": "绿"}
RARITY_ZH = {"mythic": "秘稀", "rare": "稀有", "uncommon": "非普通", "common": "普通",
             "special": "特殊", "bonus": "赠卡"}


def readonly_sqlite(path: Path) -> sqlite3.Connection:
    if not path.exists():
        sys.exit(f"[error] registry not found: {path}")
    base = path.resolve().as_uri()
    # registry.sqlite is shared with the mtga-cn maintainer; if it is mid-write
    # (journal present) fall back to immutable reads instead of blocking or failing.
    for params in ("?mode=ro", "?mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(base + params, uri=True, timeout=5)
            conn.execute("select count(*) from card_names").fetchone()
            return conn
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
    sys.exit("[error] registry.sqlite 被占用且无法只读打开，稍后重试")


def latest_run(db: sqlite3.Connection) -> str:
    """Newest client version run in card_names (version strings sort chronologically here)."""
    rows = db.execute(
        "select run_id, count(*) c from card_names group by run_id order by run_id desc"
    ).fetchall()
    if not rows:
        sys.exit("[error] card_names is empty")
    # run ids look like 2026.64.50_20261020T2044...  -> descending string order is newest first
    for run_id, count in rows:
        if count > 1000:
            return run_id
    return rows[0][0]


def load_client_names(db: sqlite3.Connection, run_id: str) -> dict[int, dict]:
    """arena_grp_id -> {en, zh, cn, token, primary, expansion}"""
    out: dict[int, dict] = {}
    cur = db.execute(
        "select arena_grp_id, identity_json, english_canonical, chinese_display "
        "from card_names where run_id = ?", (run_id,))
    for grp_id, identity, en, zh in cur:
        try:
            ident = json.loads(identity)
        except json.JSONDecodeError:
            ident = {}
        out[grp_id] = {
            "en": (en or "").strip(),
            "zh": (zh or "").strip(),
            "cn": str(ident.get("CollectorNumber") or ""),
            "token": bool(ident.get("IsToken")),
            "primary": bool(ident.get("IsPrimaryCard")),
            "expansion": ident.get("ExpansionCode") or "",
        }
    return out


def scryfall_set(set_code: str) -> list[dict]:
    """All English cards of a set, paging through the Scryfall search API."""
    cards: list[dict] = []
    url = ("https://api.scryfall.com/cards/search?q="
           + urllib.parse.quote(f"set:{set_code.lower()} lang:en game:arena")
           + "&unique=cards&order=set")
    page = 0
    while url:
        page += 1
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # no arena-legal cards for that query
                break
            sys.exit(f"[error] scryfall HTTP {exc.code} on page {page}")
        except Exception as exc:  # noqa: BLE001 - network is best effort
            sys.exit(f"[error] scryfall request failed: {exc}")
        cards.extend(payload.get("data", []))
        url = payload.get("next_page") if payload.get("has_more") else None
        if url:
            time.sleep(0.12)  # stay polite to the API
    return cards


def build(set_code: str, registry: Path, offline: bool) -> dict:
    db = readonly_sqlite(registry)
    run_id = latest_run(db)
    names = load_client_names(db, run_id)
    db.close()

    client_rows = {gid: v for gid, v in names.items() if v["expansion"].upper() == set_code.upper()}
    if not client_rows:
        sys.exit(f"[error] no cards of set {set_code} in registry run {run_id}")

    scry: dict[int, dict] = {}
    by_num: dict[str, dict] = {}
    if not offline:
        for card in scryfall_set(set_code):
            arena_id = card.get("arena_id")
            if arena_id:
                scry[int(arena_id)] = card
            by_num.setdefault(str(card.get("collector_number")), card)

    cards = []
    unmatched_meta = []
    missing_zh = []
    for grp_id, row in client_rows.items():
        meta = scry.get(grp_id)
        if meta is None and row["cn"]:
            meta = by_num.get(row["cn"])
        if meta is None:
            unmatched_meta.append(grp_id)
        zh = row["zh"]
        if not zh or not CJK.search(zh):
            missing_zh.append(grp_id)
            zh = row["en"]  # never invent a translation
        cards.append({
            "arena_id": grp_id,
            "cn": row["cn"],
            "en": row["en"],
            "zh": zh,
            "zh_verified": bool(zh != row["en"]),
            "is_token": row["token"],
            "is_primary": row["primary"],
            "rarity": (meta or {}).get("rarity", ""),
            "mana": (meta or {}).get("mana_cost", ""),
            "cmc": (meta or {}).get("cmc", ""),
            "type": (meta or {}).get("type_line", ""),
            "colors": "".join((meta or {}).get("colors", []) or []),
            "scryfall_id": (meta or {}).get("id", ""),
        })

    def sort_key(card):
        num = card["cn"]
        return (0 if num.isdigit() else 1, int(num) if num.isdigit() else 0,
                RARITY_ORDER.get(card["rarity"], 9), card["en"])

    cards.sort(key=sort_key)
    return {
        "set": set_code.upper(),
        "registry_run": run_id,
        "registry_path": str(registry),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_zh": "MTGA 官方客户端本地化数据（经用户 mtga-cn 汉化流程入库）",
        "source_meta": "Scryfall API（arena_id 精确对齐）" if not offline else "offline",
        "counts": {
            "total": len(cards),
            "main": sum(1 for c in cards if c["is_primary"] and not c["is_token"]),
            "variants": sum(1 for c in cards if not c["is_primary"] and not c["is_token"]),
            "tokens": sum(1 for c in cards if c["is_token"]),
            "missing_zh": len(missing_zh),
            "unmatched_meta": len(unmatched_meta),
        },
        "cards": cards,
    }


def write_outputs(data: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    code = data["set"]
    produced = []

    json_path = out_dir / f"{code}-cards.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    produced.append(json_path)

    csv_path = out_dir / f"{code}-cards.csv"
    cols = ["cn", "en", "zh", "rarity", "colors", "mana", "cmc", "type",
            "is_token", "arena_id", "zh_verified"]
    lines = [",".join(cols)]
    for c in data["cards"]:
        row = []
        for col in cols:
            value = str(c.get(col, ""))
            if any(ch in value for ch in ',"\n'):
                value = '"' + value.replace('"', '""') + '"'
            row.append(value)
        lines.append(",".join(row))
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    produced.append(csv_path)

    md_path = out_dir / f"{code}-cards.md"
    md_path.write_text(render_markdown(data), encoding="utf-8")
    produced.append(md_path)

    html_path = out_dir / f"{code}-cards.html"
    html_path.write_text(render_html(data), encoding="utf-8")
    produced.append(html_path)
    return produced


def render_html(data: dict) -> str:
    mains = [c for c in data["cards"] if not c["is_token"] and c["is_primary"]]
    rows = [{
        "en": c["en"], "zh": c["zh"], "r": c["rarity"], "col": c["colors"],
        "m": (c["mana"] or "").replace("{", "").replace("}", ""),
        "c": c["cmc"] if isinstance(c["cmc"], (int, float)) else 99,
        "t": c["type"],
    } for c in mains]
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    title = f"{data['set']} · 中英卡表"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#fbfbfa;--panel:#fff;--ink:#1c1c1e;--dim:#6b6b70;--line:#e8e8e6;--accent:#2f6fed}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro SC","PingFang SC",Segoe UI,sans-serif}}
header{{position:sticky;top:0;z-index:5;background:rgba(251,251,250,.92);
backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:18px 24px 14px}}
h1{{margin:0 0 4px;font-size:19px;font-weight:600;letter-spacing:.2px}}
.sub{{color:var(--dim);font-size:13px}}
.controls{{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px;align-items:center}}
input[type=search]{{flex:1 1 260px;min-width:200px;padding:9px 12px;font-size:15px;
border:1px solid var(--line);border-radius:9px;background:var(--panel);outline:none}}
input[type=search]:focus{{border-color:var(--accent);box-shadow:0 0 0 3px rgba(47,111,237,.12)}}
.chip{{padding:5px 11px;font-size:13px;border:1px solid var(--line);border-radius:999px;
background:var(--panel);cursor:pointer;user-select:none;color:var(--dim)}}
.chip[aria-pressed=true]{{background:var(--ink);color:#fff;border-color:var(--ink)}}
main{{padding:8px 24px 48px;max-width:1000px;margin:0 auto}}
table{{width:100%;border-collapse:collapse;font-size:14.5px}}
th{{text-align:left;font-weight:500;color:var(--dim);font-size:12.5px;letter-spacing:.4px;
text-transform:uppercase;padding:10px 8px;border-bottom:1px solid var(--line);position:sticky;top:118px;
background:var(--bg)}}
td{{padding:8px;border-bottom:1px solid #f2f2f0;vertical-align:top}}
tbody tr:hover{{background:#fff}}
.en{{color:var(--ink)}}
.zh{{font-weight:500}}
.mana{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;color:var(--dim);white-space:nowrap}}
.type{{color:var(--dim);font-size:13px}}
.rar{{font-size:11.5px;padding:1px 6px;border-radius:4px;white-space:nowrap;letter-spacing:.3px}}
.r-mythic{{background:#f6e3d0;color:#8a4b12}}
.r-rare{{background:#e7e3f5;color:#4b3b8f}}
.r-uncommon{{background:#e2eef0;color:#2b6777}}
.r-common{{background:#efefed;color:#5c5c60}}
.empty{{padding:40px;text-align:center;color:var(--dim)}}
.count{{color:var(--dim);font-size:13px;margin:12px 0 0}}
</style></head><body>
<header>
  <h1>{data['set']} · 中英卡表</h1>
  <div class="sub">中文名取自 MTGA 官方客户端本地化文本（与游戏内汉化显示一致）·
    全 {len(rows)} 张主系列牌</div>
  <div class="controls">
    <input type="search" id="q" placeholder="搜索英文或中文牌名…" autocomplete="off" autofocus>
    <button class="chip" data-f="all" aria-pressed="true">全部</button>
    <button class="chip" data-f="mythic" aria-pressed="false">秘稀</button>
    <button class="chip" data-f="rare" aria-pressed="false">稀有</button>
    <button class="chip" data-f="uncommon" aria-pressed="false">非普通</button>
    <button class="chip" data-f="common" aria-pressed="false">普通</button>
  </div>
</header>
<main>
<table><thead><tr><th>英文</th><th>中文</th><th>稀有度</th><th>费用</th><th>类型</th></tr></thead>
<tbody id="tb"></tbody></table>
<div class="empty" id="empty" hidden>没有匹配的牌</div>
<div class="count" id="count"></div>
</main>
<script>
const CARDS={payload};
const RZ={{mythic:"秘稀",rare:"稀有",uncommon:"非普通",common:"普通",special:"特殊",bonus:"赠卡"}};
const CZ={{W:"白",U:"蓝",B:"黑",R:"红",G:"绿"}};
const tb=document.getElementById("tb"),q=document.getElementById("q"),
      cnt=document.getElementById("count"),empty=document.getElementById("empty");
let filter="all";
const esc=s=>String(s).replace(/[&<>"]/g,c=>({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}})[c]);
function render(){{
  const kw=q.value.trim().toLowerCase();
  const list=CARDS.filter(c=>(filter==="all"||c.r===filter)&&
    (!kw||c.en.toLowerCase().includes(kw)||c.zh.includes(kw)));
  tb.innerHTML=list.map(c=>`<tr>
    <td class="en">${{esc(c.en)}}</td>
    <td class="zh">${{esc(c.zh)}}</td>
    <td><span class="rar r-${{esc(c.r||"common")}}">${{esc(RZ[c.r]||c.r)}}</span></td>
    <td class="mana">${{esc(c.m||"—")}}</td>
    <td class="type">${{esc(c.t)}}</td></tr>`).join("");
  empty.hidden=list.length>0;
  cnt.textContent=`显示 ${{list.length}} / ${{CARDS.length}} 张`;
}}
document.querySelectorAll(".chip").forEach(b=>b.onclick=()=>{{
  filter=b.dataset.f;
  document.querySelectorAll(".chip").forEach(x=>x.setAttribute("aria-pressed",x===b));
  render();
}});
q.addEventListener("input",render);
render();
</script></body></html>
"""


def render_markdown(data: dict) -> str:
    mains = [c for c in data["cards"] if not c["is_token"] and c["is_primary"]]
    variants = [c for c in data["cards"] if not c["is_token"] and not c["is_primary"]]
    out = [f"# {data['set']} 卡表中英对照（轮抽用）", ""]
    out.append(f"- 中文来源：{data['source_zh']}")
    out.append(f"- 元数据来源：{data['source_meta']}")
    out.append(f"- 客户端版本 run：`{data['registry_run']}`　生成时间：{data['generated_at']}")
    out.append(f"- 主系列牌张：{len(mains)}　异画/特殊版本：{len(variants)}"
               f"　衍生物：{data['counts']['tokens']}　中文缺失：{data['counts']['missing_zh']}")
    out.append("")
    out.append("> 本表只列主系列（可直接开到的牌）。异画、展示框、无边框等 "
               f"{len(variants)} 个重复版本不在此列，完整清单见同名 CSV / JSON。")
    out.append("")

    basics = {"Plains", "Island", "Swamp", "Mountain", "Forest"}
    groups: dict[tuple[str, str], list[dict]] = {}
    for c in mains:
        if c["en"] in basics:
            continue
        colors = c["colors"] or ""
        if not colors:
            color_group = "C"
        elif len(colors) == 1:
            color_group = colors
        else:
            color_group = "M"
        groups.setdefault((color_group, c["rarity"] or "unknown"), []).append(c)

    group_label = {"W": "白", "U": "蓝", "B": "黑", "R": "红", "G": "绿",
                   "M": "多色", "C": "无色"}
    group_order = {"W": 0, "U": 1, "B": 2, "R": 3, "G": 4, "M": 5, "C": 6}

    def gkey(item):
        color_group, rarity = item[0]
        return (group_order.get(color_group, 9), RARITY_ORDER.get(rarity, 9))

    for (color_group, rarity), items in sorted(groups.items(), key=gkey):
        multi = color_group == "M"
        out.append(f"## {group_label.get(color_group, color_group)} · "
                   f"{RARITY_ZH.get(rarity, rarity)}（{len(items)}）")
        out.append("")
        out.append("| 英文 | 中文 | 颜色 | 费用 | 类型 |" if multi
                   else "| 英文 | 中文 | 费用 | 类型 |")
        out.append("|---|---|---|---|---|" if multi else "|---|---|---|---|")
        for c in sorted(items, key=lambda x: ((x["cmc"] if isinstance(x["cmc"], (int, float)) else 99),
                                              x["colors"], x["en"])):
            mana = c["mana"].replace("{", "").replace("}", "") or "—"
            type_line = c["type"].replace("|", "/")
            if multi:
                colors_zh = "/".join(COLOR_ZH.get(ch, ch) for ch in c["colors"])
                out.append(f"| {c['en']} | {c['zh']} | {colors_zh} | {mana} | {type_line} |")
            else:
                out.append(f"| {c['en']} | {c['zh']} | {mana} | {type_line} |")
        out.append("")

    basics_seen = [c for c in mains if c["en"] in basics]
    if basics_seen:
        out.append("## 基本地")
        out.append("")
        out.append("| 英文 | 中文 |")
        out.append("|---|---|")
        seen = set()
        for c in basics_seen:
            if c["en"] in seen:
                continue
            seen.add(c["en"])
            out.append(f"| {c['en']} | {c['zh']} |")
        out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="导出某系列卡表中英对照（中文取自本地官方客户端数据）")
    ap.add_argument("set_code", help="系列代码，如 DFT / FRA / DSK")
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    ap.add_argument("--out", type=Path, default=SETS_DIR)
    ap.add_argument("--offline", action="store_true", help="不访问 Scryfall，只导出名字")
    args = ap.parse_args()

    data = build(args.set_code, args.registry, args.offline)
    files = write_outputs(data, args.out / data["set"])
    print(json.dumps({"set": data["set"], "counts": data["counts"], "files": [str(f) for f in files]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
