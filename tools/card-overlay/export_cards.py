"""Read-only Arena card/translation export for the local macOS reading panel."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import tempfile
from pathlib import Path


def readonly(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def plain(value: str) -> str:
    value = value.replace("\\n", "\n")
    value = re.sub(r"<[^>]*>", "", value)
    value = html.unescape(value)
    value = re.sub(r"\{((?:o(?:\([^)]*\)|[WUBRGCXYZSTQEP]|\d+))+)\}",
                   lambda match: mana_text(match[1]), value)
    return value.strip()


def has_chinese(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value))


def mana_text(value: str) -> str:
    return re.sub(r"o(\([^)]*\)|[WUBRGCXYZSTQEP]|\d+)",
                  lambda match: "[" + match[1].strip("()") + "] ", value).strip()


def loc_map(db, table):
    result = {}
    for locid, formatted, text in db.execute(f"SELECT LocId,Formatted,Loc FROM {table}"):
        # Display text uses F1. Other formats are fallback, never screen-size selectors.
        if locid not in result or formatted == 1:
            result[locid] = text
    return result


def export(current: Path, donor: Path, client: Path, donor_client: Path):
    with readonly(current) as db, readonly(donor) as zhdb:
        en = loc_map(db, "Localizations_enUS")
        old_en = loc_map(zhdb, "Localizations_enUS")
        old_zh = loc_map(zhdb, "Localizations_jaJP")
        # Require the donor English text to match the current version before using Chinese.
        translations = {id: plain(old_zh[id]) for id, text in en.items()
                        if id in old_en and id in old_zh and plain(text) == plain(old_en[id])
                        and has_chinese(old_zh[id])}
        label_zh = {}
        for id, text in en.items():
            if id in translations:
                label_zh.setdefault(plain(text).casefold(), translations[id])
        cards = {}
        fallback_rules = 0
        ambiguous = set()
        flavors = {}
        query = """SELECT GrpId,TitleId,TypeTextId,SubtypeTextId,FlavorTextId,
                          AbilityIds,OldSchoolManaText,Power,Toughness
                   FROM Cards WHERE IsToken=0 ORDER BY IsPrimaryCard DESC,GrpId DESC"""
        for grpid, title, typeid, subtype, flavor, abilities, mana, power, toughness in db.execute(query):
            name = plain(en.get(title, ""))
            if not name:
                continue
            rules = []
            rules_en = []
            complete = True
            for entry in (abilities or "").split(","):
                if not entry:
                    continue
                parts = entry.split(":")
                if len(parts) != 2 or not parts[1].isdigit():
                    complete = False
                    continue
                id = int(parts[1])
                english = plain(en.get(id, ""))
                if not english:
                    complete = False
                    continue
                translated = translations.get(id)
                complete &= translated is not None
                rules_en.append(english.replace("CARDNAME", name))
                rules.append((translated or english).replace("CARDNAME", translations.get(title, name)))
            if not complete:
                fallback_rules += 1
            type_parts = [x for x in [en.get(typeid, ""), en.get(subtype, "")] if x]
            type_en = " — ".join(plain(x) for x in type_parts)
            type_zh = " — ".join(translations.get(i, plain(en.get(i, "")))
                                  for i in [typeid, subtype] if en.get(i))
            card = {"name": name, "nameZh": translations.get(title, name), "arenaId": grpid,
                    "typeEn": type_en, "typeZh": type_zh,
                    "rules": "\n".join(dict.fromkeys(rules)),
                    "rulesEn": "\n".join(dict.fromkeys(rules_en)),
                    "translationComplete": bool(complete),
                    "flavorEn": plain(en.get(flavor, "")), "mana": mana_text(mana or ""),
                    "stats": f"{power}/{toughness}" if power and toughness else ""}
            if name in cards and cards[name]["rulesEn"] != card["rulesEn"]:
                ambiguous.add(name)
            flavors.setdefault(name, set()).add(card["flavorEn"])
            cards.setdefault(name, card)
        # OCR cannot distinguish same-name variants with different rules. Fail closed.
        for name in ambiguous:
            cards.pop(name, None)
        for name, variants in flavors.items():
            if name in cards and len(variants) > 1:
                # Name OCR cannot identify the print. Native flavor remains visible in English.
                cards[name]["flavorEn"] = ""
        versions = dict(db.execute("SELECT * FROM Versions"))

    with readonly(client) as db, readonly(donor_client) as zhdb:
        old = {k: (e, z) for k, e, z in zhdb.execute("SELECT Key,enUS,jaJP FROM Loc")}
        glossary = []
        for key, english in db.execute("SELECT Key,enUS FROM Loc WHERE Key LIKE 'AbilityHanger/Keyword/%'"):
            if key not in old or english != old[key][0] or not has_chinese(old[key][1]):
                continue
            tail = key.rsplit("/", 1)[1]
            if not re.fullmatch(r"[A-Za-z]+", tail):
                continue
            title = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tail)
            translated_title = label_zh.get(title.casefold())
            if not translated_title:
                continue
            glossary.append({"name": title, "nameZh": translated_title,
                             "explanationEn": plain(english), "explanationZh": plain(old[key][1])})
        for card in cards.values():
            text = card["rulesEn"].casefold()
            card["keywords"] = [g for g in glossary if re.search(
                r"(?<!\w)" + re.escape(g["name"].casefold()) + r"(?!\w)", text)]
    return {"schema": 1, "versions": versions, "cards": list(cards.values()),
            "sources": [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                        for p in [current, donor, client, donor_client]],
            "summary": {"cardNames": len(cards), "keywords": len(glossary),
                        "ambiguousNamesExcluded": len(ambiguous),
                        "printingRulesWithEnglishFallback": fallback_rules}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--arena-root", type=Path, default=Path.home() / "Library/Application Support/Steam/steamapps/common/MTGA")
    parser.add_argument("--donor-root", type=Path, default=Path.home() / "Downloads/MTGA Mod(MacOS) 26.9.9/Raw")
    args = parser.parse_args()
    raw = args.arena_root / "MTGA_Data/Downloads/Raw"
    def newest(root, pattern):
        files = sorted(root.glob(pattern), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        if not files:
            parser.error(f"Missing local resource: {root / pattern}")
        return files[0]
    result = export(newest(raw, "Raw_CardDatabase_*.mtga"),
                    newest(args.donor_root, "Raw_CardDatabase_*.mtga"),
                    newest(raw, "Raw_ClientLocalization_*.mtga"),
                    newest(args.donor_root, "Raw_ClientLocalization_*.mtga"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent,
                                     prefix=".cards-", delete=False) as pending:
        json.dump(result, pending, ensure_ascii=False, separators=(",", ":"))
        temp_path = Path(pending.name)
    temp_path.chmod(0o600)
    temp_path.replace(args.output)
    print(json.dumps(result["summary"]))


if __name__ == "__main__":
    main()
