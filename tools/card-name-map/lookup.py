#!/usr/bin/env python3
"""Look up Chinese card names for a MTG Arena set, offline.

Usage:
  mtga-cardmap "Canyon Vaulter" "Broadcast Rambler"
  mtga-cardmap --scan pack.txt          # pull every card name out of a log/pack dump
  echo "Air Response Unit" | mtga-cardmap -
  mtga-cardmap --set FRA --list uncommon

Data is read from sets/<CODE>/<CODE>-cards.json produced by build_set.py.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SETS_DIR = HERE / "sets"
RARITY_ZH = {"mythic": "秘稀", "rare": "稀有", "uncommon": "非普通",
             "common": "普通", "special": "特殊", "bonus": "赠卡"}
COLOR_ZH = {"W": "白", "U": "蓝", "B": "黑", "R": "红", "G": "绿"}


def available_sets() -> list[tuple[str, float, Path]]:
    out = []
    for path in SETS_DIR.glob("*/*-cards.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append((data.get("set", path.stem), path.stat().st_mtime, path))
    return sorted(out, key=lambda item: item[1], reverse=True)


def current_set_code() -> str | None:
    """Series currently on Quick Draft, per current-quickdraft.json."""
    path = HERE / "current-quickdraft.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8"))["current"]["set_code"])
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def load(set_code: str | None) -> dict:
    sets = available_sets()
    if not sets:
        sys.exit("[error] 没有任何卡表，先运行：build_set.py <SET_CODE>")
    if set_code:
        for code, _, path in sets:
            if code.upper() == set_code.upper():
                return json.loads(path.read_text(encoding="utf-8"))
        sys.exit(f"[error] 未找到系列 {set_code}；已有：{', '.join(c for c, _, _ in sets)}")
    active = current_set_code()
    if active:
        for code, _, path in sets:
            if code.upper() == active.upper():
                return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(sets[0][2].read_text(encoding="utf-8"))


def fmt(card: dict) -> str:
    bits = [f"{card['en']} = {card['zh']}"]
    tail = []
    if card.get("rarity"):
        tail.append(RARITY_ZH.get(card["rarity"], card["rarity"]))
    colors = card.get("colors") or ""
    if colors:
        tail.append("/".join(COLOR_ZH.get(ch, ch) for ch in colors))
    elif card.get("type"):
        tail.append("无色")
    mana = (card.get("mana") or "").replace("{", "").replace("}", "")
    if mana:
        tail.append(mana)
    if card.get("type"):
        tail.append(card["type"])
    if tail:
        bits.append("｜" + " ｜".join(tail))
    return "".join(bits)


def index(data: dict):
    cards = [c for c in data["cards"] if not c["is_token"]]
    en = {}
    zh = {}
    grp = {}
    for card in cards:
        en.setdefault(card["en"].casefold(), card)
        zh.setdefault(card["zh"].casefold(), card)
        grp[int(card["arena_id"])] = card
    return cards, en, zh, grp


def resolve(query: str, cards, en, zh) -> tuple[dict | None, str]:
    q = query.strip().strip('",.;:()[]')
    if not q:
        return None, ""
    key = q.casefold()
    if key in en:
        return en[key], "exact"
    if key in zh:
        return zh[key], "exact-zh"
    # Arena logs sometimes carry "Card Name (set) 1234" or trailing markers
    stripped = re.sub(r"\s*[\(\[].*?[\)\]]\s*$", "", q).strip()
    if stripped.casefold() in en:
        return en[stripped.casefold()], "exact"
    hits = [c for c in cards if key in c["en"].casefold()]
    if len(hits) == 1:
        return hits[0], "substring"
    if hits:
        hits.sort(key=lambda c: len(c["en"]))
        return hits[0], f"substring(+{len(hits) - 1})"
    names = list(en)
    close = difflib.get_close_matches(key, names, n=1, cutoff=0.72)
    if close:
        return en[close[0]], "fuzzy"
    return None, ""


def scan(text: str, cards) -> list[tuple[str, dict | None]]:
    """Find known card names inside arbitrary text, longest match first."""
    lowered = text.casefold()
    ordered = sorted(cards, key=lambda c: len(c["en"]), reverse=True)
    taken = [False] * len(lowered)
    found: list[tuple[int, str, dict]] = []
    for card in ordered:
        needle = card["en"].casefold()
        if len(needle) < 3:
            continue
        start = 0
        while True:
            pos = lowered.find(needle, start)
            if pos < 0:
                break
            end = pos + len(needle)
            if not any(taken[pos:end]):
                for i in range(pos, end):
                    taken[i] = True
                found.append((pos, card["en"], card))
            start = pos + 1
    found.sort(key=lambda item: item[0])
    return [(name, card) for _, name, card in found]


def main() -> None:
    ap = argparse.ArgumentParser(description="英文牌名 -> 中文牌名（离线）")
    ap.add_argument("names", nargs="*", help="英文牌名；用 - 表示从标准输入读取文本")
    ap.add_argument("--set", dest="set_code", help="系列代码，默认使用最新生成的卡表")
    ap.add_argument("--scan", action="store_true", help="把输入当作整段文本，抽出其中所有牌名")
    ap.add_argument("--grp", action="store_true",
                    help="输入为 Arena GrpId（日志里的 DraftPack 数字），逗号/空格分隔均可")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--list", metavar="RARITY", help="列出某稀有度全部牌（common/uncommon/rare/mythic）")
    args = ap.parse_args()

    data = load(args.set_code)
    cards, en, zh, grp = index(data)

    if args.list:
        want = args.list.casefold()
        picked = [c for c in cards if c.get("rarity", "").casefold() == want]
        for card in sorted(picked, key=lambda c: (c["cmc"] if isinstance(c["cmc"], (int, float)) else 99, c["en"])):
            print(fmt(card))
        print(f"# {data['set']} {RARITY_ZH.get(want, want)}：{len(picked)} 张")
        return

    args_in = list(args.names)
    if not args_in or "-" in args_in:
        args_in = [a for a in args_in if a != "-"]
        args_in.append(sys.stdin.read())

    grp_ids: list[int] = []
    if args.grp:
        for chunk in args_in:
            for piece in re.split(r"[^0-9]+", chunk):
                if piece:
                    grp_ids.append(int(piece))
        args_in = []

    results = []
    if args.grp:
        for gid in grp_ids:
            card = grp.get(gid)
            results.append({"query": str(gid), "match": "grpid" if card else "missing", "card": card})
    for chunk in args_in:
        if args.scan or "\n" in chunk or len(chunk) > 120:
            for name, card in scan(chunk, cards):
                results.append({"query": name, "match": "scan", "card": card})
        else:
            card, how = resolve(chunk, cards, en, zh)
            results.append({"query": chunk, "match": how, "card": card})

    if args.json:
        print(json.dumps({"set": data["set"], "results": results}, ensure_ascii=False, indent=1))
        return

    misses = 0
    for item in results:
        card = item["card"]
        if card is None:
            misses += 1
            print(f"{item['query']} = ?  未在 {data['set']} 卡表中找到")
        else:
            print(fmt(card))
    if misses:
        print(f"\n# {misses} 条未命中；可用 --set 指定系列，或确认拼写", file=sys.stderr)


if __name__ == "__main__":
    main()
