#!/usr/bin/env python3
"""Cloud discovery -> bot notification -> local maintenance -> signed subscription.

Only public Steam/Wizards metadata and the actual signed subscriber feed are read.
No player login, game payload download, translation, build or installation occurs.
"""
import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
REPO = "1996Neil/mtga-chinese-updates"
STATE_PATH = "mtga-watch/state.json"
STATE_SCHEMA = "MTGA_PUBLIC_WATCH_STATE_V1"
MAX_SEEN_KEYS = 10000
CDN = "https://assets.mtgarena.wizards.com/"
STATUS_ROOT = "https://magicthegatheringarena.statuspage.io/api/v2/"
STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
APP_ID, DEPOT_ID = "2141910", "2141913"
UA = "mtga-update-watch/2"
VERSION_HINT = re.compile(r"\b(update|patch|release|new version|client update)\b", re.I)
HEX32 = re.compile(r"[0-9a-f]{32}")
HEX64 = re.compile(r"[0-9a-f]{64}")
LAYERS = ("card", "client", "font_bundle")
ALLOWED_HOSTS = {"assets.mtgarena.wizards.com", "magicthegatheringarena.statuspage.io",
                 "steamcdn-a.akamaihd.net", "raw.githubusercontent.com"}


def log(message):
    print(message, flush=True)


class StrictRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(req.full_url).hostname == "api.github.com":
            raise ValueError("GitHub API redirect refused")
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_url(url):
    u = urllib.parse.urlsplit(url)
    if (u.scheme != "https" or u.hostname not in ALLOWED_HOSTS or u.port not in (None, 443)
            or u.username or u.password or u.query or u.fragment):
        raise ValueError("Unexpected metadata URL")


def fetch(url, timeout=45, limit=32 * 1024 * 1024):
    validate_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.build_opener(StrictRedirect).open(request, timeout=timeout) as response:
        validate_url(response.url)
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Metadata response too large")
        return data


def github_call(method, tail, payload=None):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN missing")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/{tail}",
        data=json.dumps(payload).encode() if payload is not None else None, method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.build_opener(StrictRedirect).open(request, timeout=45) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("GitHub response too large")
        return json.loads(raw) if raw else None


def verified_envelope(raw, public_key):
    envelope = json.loads(raw)
    payload = base64.b64decode(envelope["payload"], validate=True)
    signature = base64.b64decode(envelope["signature"], validate=True)
    public = base64.b64decode(public_key, validate=True)
    if len(public) != 32 or len(signature) != 64:
        raise ValueError("Invalid Ed25519 envelope")
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        (p / "key.der").write_bytes(bytes.fromhex("302a300506032b6570032100") + public)
        (p / "payload").write_bytes(payload)
        (p / "signature").write_bytes(signature)
        result = subprocess.run(["openssl", "pkeyutl", "-verify", "-rawin", "-pubin",
            "-keyform", "DER", "-inkey", str(p / "key.der"), "-in", str(p / "payload"),
            "-sigfile", str(p / "signature")], capture_output=True, timeout=15)
        if result.returncode:
            raise ValueError("Subscriber feed signature verification failed")
    return json.loads(payload)


def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else None
    except (ValueError, AttributeError):
        return None


def release_order(ref):
    version = ref["client_version"]
    build, revision = ref["steam_build"], ref["revision"]
    if (not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", version)
            or not isinstance(build, str) or not build.isdigit()
            or type(revision) is not int or revision < 1):
        raise ValueError("Invalid release identity")
    return tuple(map(int, version.split("."))), int(build), revision


def baseline_target(config, now):
    feed = config["feed"].rstrip("/")
    expected = "https://raw.githubusercontent.com/1996Neil/mtga-chinese-updates/main/feed"
    if feed != expected or config["channel"] not in ("testing", "stable"):
        raise ValueError("Unexpected subscriber feed")
    raw = fetch(f"{feed}/index-{config['channel']}.json", limit=2 * 1024 * 1024)
    index = verified_envelope(raw, config["public_key"])
    if (index.get("schema") != "MTGA_CN_INDEX_V1" or index.get("channel") != config["channel"]
            or type(index.get("sequence")) is not int or index["sequence"] < 1):
        raise ValueError("Invalid subscriber index")
    expires = parse_time(index.get("expires_at"))
    if not expires or expires <= now:
        raise ValueError("Published subscriber index expired")
    refs = index["releases"]
    if not isinstance(refs, list) or not refs or len(refs) > 10000:
        raise ValueError("No published releases")
    identities = [release_order(r) for r in refs]
    if len(set(identities)) != len(identities) or len({r["id"] for r in refs}) != len(refs):
        raise ValueError("Duplicate published release identity")
    ref = max(refs, key=release_order)
    name = ref["manifest"]["name"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.json", name):
        raise ValueError("Invalid release manifest filename")
    raw = fetch(f"{feed}/{name}", limit=2 * 1024 * 1024)
    fp = ref["manifest"]["fingerprint"]
    if {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()} != fp:
        raise ValueError("Published release manifest fingerprint mismatch")
    release = verified_envelope(raw, config["public_key"])
    if (release.get("schema") != "MTGA_CN_RELEASE_V1" or release.get("platform") != "macOS-Steam"
            or any(release.get(k) != ref[k] for k in ("id", "client_version", "steam_build", "revision"))):
        raise ValueError("Published release identity mismatch")
    resources = release["resources"]
    if len(resources) != 5 or {r["layer"] for r in resources} != {"card", "client", "font_bundle", "resources", "bootstrap_fonts"}:
        raise ValueError("Published release layer set mismatch")
    return {"release_id": release["id"], "client_version": release["client_version"],
            "steam_build": release["steam_build"], "revision": release["revision"],
            "index_sequence": index["sequence"], "expires_at": index["expires_at"],
            "feed": feed, "signature_verified": True, "resources": resources}


def statuspage_items():
    # Include recent completed notices, not only events active at polling time.
    items = []
    for endpoint, field in (("incidents.json", "incidents"),
                            ("scheduled-maintenances.json", "scheduled_maintenances")):
        data = json.loads(fetch(STATUS_ROOT + endpoint, timeout=30, limit=2 * 1024 * 1024))
        for row in data[field][:100]:
            name = row.get("name", "")
            if not VERSION_HINT.search(name):
                continue
            items.append({"id": row["id"], "name": name, "status": row["status"],
                          "announced_at": parse_time(row.get("created_at")),
                          "starts_at": parse_time(row.get("scheduled_for")),
                          "ends_at": parse_time(row.get("scheduled_until")),
                          "updated_at": parse_time(row.get("updated_at")),
                          "body": str((row.get("incident_updates") or [{}])[0].get("body") or "")[:4000],
                          "kind": field})
    return items


def relevant(item, now, lookback):
    announced, starts = item.get("announced_at"), item.get("starts_at")
    if announced and timedelta(0) <= now - announced <= lookback:
        return "新公告"
    if starts and timedelta(0) < starts - now <= timedelta(hours=12):
        return "即将维护（不代表新版已部署）"
    return None


NOTICE_DONE = {"completed", "resolved"}
NOTICE_ACTIVE = {"in_progress", "verifying", "monitoring", "investigating", "identified"}


def beijing(value):
    return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M（北京时间）") if value else "官方未提供"


def notice_finding(item, now, lookback):
    """An estimate is a check time, never a promise of installable localization."""
    status = item.get("status", "scheduled")
    announced, starts, ends = (item.get(k) for k in ("announced_at", "starts_at", "ends_at"))
    done = status in NOTICE_DONE
    if done:
        # A completion update can be recent even when the original announcement is old.
        stamp = item.get("updated_at") or announced
        if not stamp or not timedelta(0) <= now - stamp <= lookback:
            return None
    elif not relevant(item, now, lookback) and status not in NOTICE_ACTIVE:
        return None
    lines = ["官方公告：" + item["name"], "公告发布时间：" + beijing(announced),
             "计划维护开始：" + beijing(starts), "计划维护结束：" + beijing(ends)]
    if starts and ends and ends > starts:
        hours = (ends - starts).total_seconds() / 3600
        lines.append(f"官方预计维护时长：{hours:g} 小时（可能提前或延长）。")
    if done:
        key = "notice-completed-" + item["id"]
        title = "维护进展：官方已宣布完成 · " + item["name"][:80]
        lines += ["官方已宣布维护完成。现在可以打开 Steam 和游戏检查官方更新，下载完成后退出游戏，再核对资源。",
                  "这不证明汉化相关资源已变化，也不代表汉化包已可安装；资源变化另行通知。"]
    else:
        # Preserve the existing announcement marker, including previously closed issues.
        key = "notice-" + item["id"]
        title = "维护预告：" + item["name"][:80]
        if status in NOTICE_ACTIVE:
            lines.append("官方维护正在进行或验证中；请等待完成公告或新资源确认。")
        if ends and ends > now:
            hours = (ends - now).total_seconds() / 3600
            lines.append(f"距预计结束约 {hours:.1f} 小时；可在上述结束时间后检查官方更新，届时仍需确认实际维护状态。")
        elif ends:
            lines.append("原定结束时间已过，但尚未确认完成；不要仅按倒计时开始替换。")
        else:
            lines.append("官方未提供预计结束时间，无法可靠给出几小时后可更新。")
        lines.append("行动建议：先等待官方部署和下载；公告内容仅供预判，不能据此确定实际资源变化或汉化完成时间。")
    if item.get("body"):
        lines.append("官方最新说明（原文）：\n" + item["body"])
    lines += ["官方公告来源：https://magicthegatheringarena.statuspage.io/",
              "监控每 6 小时检查一次，GitHub 排队可能使检查延迟；相对小时数以本次检查时间为准。",
              "汉化需要多久，要在下载后对比新增文本、格式和验证结果，当前无法可靠估时。"]
    return finding(key, title, "\n".join(lines))


def steam_buildid():
    """匿名读 Steam 官方 app 信息，返回 (buildid, manifest_gid)。"""
    with tempfile.TemporaryDirectory() as workdir:
        work = Path(workdir)
        archive = work / "steamcmd.tar.gz"
        log("下载 steamcmd 引导包…")
        archive.write_bytes(fetch(STEAMCMD_URL))
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(work / "boot", filter="data")
        log("读取官方 app 信息（匿名登录，不使用任何用户凭据）…")
        result = subprocess.run(
            [str(work / "boot" / "steamcmd.sh"),
             "+@ShutdownOnFailedCommand", "1", "+@NoPromptForPassword", "1",
             "+login", "anonymous", "+app_info_update", "1",
             "+app_info_print", APP_ID, "+quit"],
            cwd=work / "boot", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=480,
        )
        text = result.stdout.decode("utf-8", "replace")
        if result.returncode != 0:
            raise RuntimeError(f"steamcmd 退出码 {result.returncode}")
        match = re.search(r'(?m)^"2141910"\s*\n\s*\{', text)
        if not match:
            raise RuntimeError("输出里没有找到 2141910 的元数据块")
        brace = text.index("{", match.start())
        depth = 0
        quoted = escaped = False
        end = None
        for index in range(brace, len(text)):
            char = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end is None:
            raise RuntimeError("元数据块没有闭合")
        return parse_vdf(text[match.start():end])


def parse_vdf(block):
    tokens = re.findall(r'"(?:[^"\\]|\\.)*"|[{}]|[^\s{}"]+', block)

    def value(token):
        if token.startswith('"'):
            return token[1:-1].replace("\\\\", "\\").replace('\\"', '"')
        return token

    def parse(position):
        if position >= len(tokens) or tokens[position] != "{":
            raise ValueError("Invalid VDF object")
        data = {}
        position += 1
        while tokens[position] != "}":
            key = value(tokens[position])
            if key in data:
                raise ValueError("Duplicate Steam VDF key")
            position += 1
            if tokens[position] == "{":
                item, position = parse(position)
            else:
                item = value(tokens[position])
                position += 1
            data[key] = item
        return data, position + 1

    if not tokens or value(tokens[0]) != APP_ID:
        raise ValueError("Wrong Steam app identity")
    app, end = parse(1)
    if end != len(tokens):
        raise ValueError("Trailing Steam VDF data")
    depots = app["depots"]
    if depots[DEPOT_ID]["config"]["oslist"] != "macos":
        raise ValueError("Steam depot is not macOS")
    public = depots["branches"]["public"]
    manifest = depots[DEPOT_ID]["manifests"]["public"]
    if not public["buildid"].isdigit() or not manifest["gid"].isdigit():
        raise ValueError("Invalid Steam build/depot identity")
    return public["buildid"], manifest["gid"]



def manifest(name):
    if not re.fullmatch(r"Manifest_(?:Localization_)?[0-9a-f]{32}\.mtga", name):
        raise ValueError("Invalid official manifest name")
    raw = fetch(CDN + name, limit=16 * 1024 * 1024)
    if raw[:2] == b"\x1f\x8b":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        plain = decoder.decompress(raw, 32 * 1024 * 1024 + 1)
        if len(plain) > 32 * 1024 * 1024 or not decoder.eof or decoder.unused_data:
            raise ValueError("Invalid or oversized manifest compression")
    else:
        plain = raw
    if hashlib.md5(plain).hexdigest() != name[:-5].rsplit("_", 1)[-1]:
        raise ValueError("Official manifest content-address mismatch")
    result = {}
    for asset in json.loads(plain)["Assets"]:
        layer = ("card" if asset["Name"].startswith("Raw_CardDatabase_") else
                 "client" if asset["Name"].startswith("Raw_ClientLocalization_") else
                 "font_bundle" if asset["Name"].startswith("Fonts_") else None)
        if layer:
            sha = base64.b64decode(asset["sha256"], validate=True).hex()
            if layer in result or not HEX64.fullmatch(sha) or type(asset["Length"]) is not int or asset["Length"] < 1:
                raise ValueError("Invalid/duplicate official resource entry")
            result[layer] = {"name": asset["Name"], "sha256": sha, "bytes": asset["Length"]}
    return result


def resource_generation(seed):
    filename = seed["external_name"]
    if not re.fullmatch(r"External_[0-9]+(?:\.[0-9]+){4}-macos\.mtga", filename):
        raise ValueError("Invalid observed macOS external pointer")
    pointer = fetch(CDN + filename, limit=128).decode().strip()
    if not HEX32.fullmatch(pointer):
        raise ValueError("Invalid official main pointer")
    main = manifest("Manifest_" + pointer + ".mtga")
    loc = manifest(seed["localization_manifest"])
    if set(main) != {"card", "font_bundle"} or set(loc) != {"client"}:
        raise ValueError("Official manifest resource layers incomplete")
    return {"assets": {**main, **loc}, "main_manifest": "Manifest_" + pointer + ".mtga",
            "localization_manifest": seed["localization_manifest"],
            "seed_steam_build": seed["steam_build"], "seed_client_version": seed["client_version"],
            "observed_against_release": seed["published_release_id"],
            "external_pointer_checked_live": True,
            "independent_localization_latest": "UNVERIFIED_REQUIRES_NEW_OFFICIAL_POINTER",
            "scope": "Live main pointer for observed macOS version; known independent localization manifest only. Not global latest."}


def finding(key, title, text, legacy_title=None):
    return {"key": key, "title": "[MTGA] " + title, "text": text, "legacy_title": legacy_title}


def compare_resources(baseline, observed):
    if int(observed["seed_steam_build"]) < int(baseline["steam_build"]):
        return [finding("resource-seed-stale-" + baseline["release_id"], "监控资源指针需要刷新",
                        "已发布版本比本地观测种子新，不能把旧资源当作官方新版。请在本地更新完成后导出并同步资源指针。")]
    published = {r["layer"]: r for r in baseline["resources"]}
    changed = []
    for layer, asset in observed["assets"].items():
        row = published[layer]
        if Path(row["relative"]).name != asset["name"] or row["input"] != {k: asset[k] for k in ("sha256", "bytes")}:
            changed.append(layer)
    if not changed:
        return []
    # Independent localization manifests are immutable. After a new publication,
    # an old observation cannot establish which direction a mismatch occurred.
    if observed["observed_against_release"] != baseline["release_id"]:
        return [finding("resource-seed-publication-" + baseline["release_id"], "发布后资源观测需要刷新",
            "订阅索引已经推进，旧观测与当前发布资源不同。请重新导出资源观测；不能把冻结的旧语言包当成新热更新。")]
    digest = hashlib.sha256(json.dumps(observed["assets"], sort_keys=True).encode()).hexdigest()[:16]
    return [finding("resources-" + digest, "资源已确认变化，可开始本地适配：" + "、".join(changed),
                    "当前观测资源世代不匹配订阅版本 " + baseline["release_id"] +
                    "；已确认这些清单记录的资源身份发生变化，可以开始本地适配检查。\n"
                    "先让 Steam 和游戏完成官方资源下载，再完全退出游戏，对比原文并构建。\n"
                    "资源已可查询不代表官方维护已结束，也不代表所有新资源已下载完成或汉化包已可安装。\n"
                    "此检查不依赖 Steam build 是否变化；独立语言包仅覆盖已知指针。")]


def all_watch_issues():
    issues = []
    for page in range(1, 101):
        rows = github_call("GET", f"issues?state=all&per_page=100&page={page}") or []
        # A public visitor may open an issue with a forged marker. Only the
        # repository owner and our Actions bot can acknowledge monitor events.
        trusted = {REPO.split("/")[0], "github-actions[bot]"}
        issues.extend(i for i in rows if "pull_request" not in i
                      and i.get("title", "").startswith("[MTGA]")
                      and (i.get("user") or {}).get("login") in trusted)
        if len(rows) < 100:
            return issues
    raise ValueError("Issue history exceeded bound; refuse incomplete deduplication")


def notify_issue(item, body, issues):
    marker = "<!-- mtga-watch:" + item["key"] + " -->"
    for issue in issues:
        body_text = issue.get("body") or ""
        legacy_match = (item["key"].startswith("steam-build-") and item.get("legacy_title")
                        and "<!-- mtga-watch:" not in body_text
                        and issue.get("title") == item["legacy_title"])
        if marker in body_text or legacy_match:
            log(f"已有事件 issue #{issue['number']}（包括已关闭），不重复通知。")
            return {"status": "deduplicated", "issue": issue["number"], "url": issue.get("html_url")}
    created = github_call("POST", "issues", {"title": item["title"],
        "body": f"@{REPO.split('/')[0]}\n\n{marker}\n\n{body}"})
    issues.append(created)
    return {"status": "bot_issue_created_email_receipt_unverified", "issue": created["number"],
            "url": created["html_url"]}


def instructions(item, report):
    baseline = report.get("probes", {}).get("published_feed", {}).get("release_id", "未能核实")
    return (item["text"] + "\n\n检查时间：" + report.get("checked_at", "未记录") + "（UTC；相对时间以此为准）" +
        "\n当前已发布订阅版本：" + baseline +
        "\n\n维护流程：\n"
        "1. 等 Steam 和游戏内资源下载完成，完全退出游戏。\n"
        "2. 本地检查新版官方底稿和备份，对比新增、修改、删除的英文与牌面身份。\n"
        "3. 复用已验证译文；大学院废墟等社区资料先核对原文、用途和格式，未知项保留英文。\n"
        "4. 构建五层候选，检查中英导入、非目标数据和字体缓存；完成审查及实际必要验收。\n"
        "5. 核对源码后，用发布器签名并发布更新包、原子推进订阅索引；索引可用后才发布用户 Release。\n"
        "6. 所有用户使用同一订阅工具检查、预演、备份和应用；你也使用这个入口。\n\n"
        "监控基准直接读取用户的签名订阅索引，未发布的本地候选不算新版可用。\n"
        "状态页公告是预警；build、macOS depot 和资源清单分别是不同信号。\n"
        "独立语言包最新指针尚无匿名发现能力：已知清单可验证，不承诺覆盖全部同 build 热更新。\n"
        "维护者在 Actions summary 和 mtga-watch/state.json 查看监控结果；启用维护者通知时，真实新事件建 issue 并 @维护者。普通用户只订阅真正可用的 Releases。\n\n"
        "运行报告：" + report["run_url"])


def write_report(report, destination):
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Path(destination).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write("### MTGA 更新监控\n\n")
            f.write("状态：" + report["status"] + "\n\n")
            for item in report["findings"]:
                f.write("- " + item["title"].replace("\n", " ") + "\n")
            f.write("\n独立语言包 global latest：尚未验证。详见 watch-report.json。\n")


def validate_state(state):
    if (not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA
            or state.get("repository") != REPO or not isinstance(state.get("seen_keys"), list)
            or len(state["seen_keys"]) > MAX_SEEN_KEYS
            or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", k)
                   for k in state["seen_keys"])
            or len(set(state["seen_keys"])) != len(state["seen_keys"])):
        raise ValueError("Invalid/incomplete public watch state; refuse to reset deduplication")
    return state


def remote_state():
    """Read the fixed file on main; missing state is an error, never an empty seed."""
    value = github_call("GET", "contents/" + STATE_PATH + "?ref=main")
    if (value.get("type") != "file" or value.get("encoding") != "base64"
            or value.get("path") != STATE_PATH or not re.fullmatch(r"[0-9a-f]{40}", value.get("sha", ""))):
        raise ValueError("Unexpected public watch state object")
    raw = base64.b64decode("".join(value["content"].split()), validate=True)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Public watch state too large")
    return validate_state(json.loads(raw)), value["sha"]


def advance_state(state, findings, report, acknowledged_keys=()):
    validate_state(state)
    seen = set(state["seen_keys"])
    events = []
    for item in findings:
        key = item["key"]
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", key):
            raise ValueError("Invalid observation key")
        events.append({"key": key, "status": "previously-observed" if key in seen else "new-observation"})
    allowed = {item["key"] for item in findings}
    if not set(acknowledged_keys) <= allowed:
        raise ValueError("Acknowledged event was not observed in this run")
    seen.update(acknowledged_keys)
    if len(seen) > MAX_SEEN_KEYS:
        raise ValueError("Watch state exceeded bound; do not truncate old deduplication")
    next_state = dict(state)
    next_state.update({"seen_keys": sorted(seen), "checked_at": report["checked_at"],
        "status": report["status"], "run_url": report["run_url"],
        "findings": [{"key": x["key"], "title": x["title"], "details": instructions(x, report)} for x in findings],
        "errors": list(report["errors"]), "unnotified_events_before_run": sum(e["status"] == "new-observation" for e in events),
        "human_notifications_sent": report.get("human_notifications_sent", False), "localization_published": False})
    return validate_state(next_state), events


def persist_state(state, expected_sha):
    """Single-file optimistic CAS. Concurrent main commits are preserved by API.

    A 409/422 fails this run. Never fetch-and-overwrite or force retry after a
    conflicting state update; the next scheduled run starts from the fresh seed.
    """
    validate_state(state)
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("Expected state blob SHA required")
    raw = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Public watch state too large")
    return github_call("PUT", "contents/" + STATE_PATH, {
        "message": "Update public MTGA maintenance observations [skip ci]",
        "content": base64.b64encode(raw).decode(), "sha": expected_sha, "branch": "main"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-steam", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--notify-maintainer", action="store_true",
                        help="Create issues only for new events and only when configured; never used by a dry-run")
    parser.add_argument("--lookback-hours", type=float, default=24)
    parser.add_argument("--state-file", type=Path, default=ROOT / "state.json",
                        help="Read-only local deduplication seed used by --dry-run")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--report", type=Path, default=Path("watch-report.json"))
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    report = {"schema": "MTGA_UPDATE_WATCH_REPORT_V2", "checked_at": now.isoformat(),
        "run_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
        "probes": {}, "findings": [], "errors": [], "notifications": [],
        "source_private": False, "game_files_changed": False, "localization_published": False,
        "human_notifications_sent": False, "notification_channel": "summary-public-state-and-opt-in-maintainer-issues"}
    config = json.loads(args.config.read_text())
    findings = report["findings"]

    def probe(name, action):
        try:
            value = action()
            report["probes"][name] = value
            return value
        except Exception as error:
            # Do not log HTTP request headers, tokens, response bodies, or credentials.
            code = str(error.code) if isinstance(error, urllib.error.HTTPError) else type(error).__name__
            report["errors"].append({"probe": name, "error": code})
            findings.append(finding("probe-failure-" + name, "监控异常：" + name,
                                    "检查失败（" + code + "），不能据此认定没有更新。请查看本次运行报告。"))
            return None

    baseline = probe("published_feed", lambda: baseline_target(config, now))
    items = probe("statuspage", statuspage_items)
    if items is not None:
        report["probes"]["statuspage"] = {"checked": True, "version_notices": len(items),
            "notices": [{k: v.isoformat() if isinstance(v, datetime) else v for k, v in item.items()} for item in items]}
        for item in items:
            notice = notice_finding(item, now, timedelta(hours=args.lookback_hours))
            if notice:
                findings.append(notice)
    seed = config["resource_probe"]
    if args.with_steam:
        steam = probe("steam", steam_buildid)
        if steam:
            build, gid = steam
            report["probes"]["steam"] = {"steam_build": build, "macos_manifest": gid, "anonymous": True}
            log(f"Steam 官方：build {build} / macOS manifest {gid}")
            if baseline and build != baseline["steam_build"]:
                findings.append(finding("steam-build-" + build, "新版本 build " + build,
                    "Steam 官方 build " + build + " 不同于已发布补丁 " + baseline["steam_build"] +
                    "。可以检查并下载官方更新；下载后核对新资源，不能据此认定汉化可安装或相关文本一定变化。",
                    f"[MTGA] 新版本 build {build}（当前固定 {baseline['steam_build']}）"))
            if build != seed["steam_build"]:
                findings.append(finding("resource-seed-build-" + build, "新 build 的资源指针待本地观测",
                    "Steam 已是 build " + build + "，当前资源种子仍属 " + seed["steam_build"] +
                    "。本轮资源查询不能证明新 build 的资源已被覆盖。"))
            if build == seed["steam_build"] and gid != seed["macos_manifest"]:
                findings.append(finding("macos-manifest-" + gid, "同 build 的 macOS depot 已变化",
                    "Steam build 不变，但 macOS depot manifest 与最近本地官方观测不同。"))
    observed = probe("resources", lambda: resource_generation(seed))
    if baseline and observed:
        findings.extend(compare_resources(baseline, observed))
        if baseline["steam_build"] != seed["steam_build"]:
            report["probes"]["resources"]["seed_covers_published_build"] = False
        log("签名订阅基准：" + baseline["release_id"])
    report["status"] = "DEGRADED" if report["errors"] else "CHECKED_WITH_LOCALIZATION_DISCOVERY_LIMIT"
    # The consolidated monitor retains private-monitor event keys without
    # replaying them. Owner issue notifications are explicitly enabled by the
    # scheduler/config; ordinary users subscribe only to ready Releases.
    try:
        if args.dry_run:
            state = validate_state(json.loads(args.state_file.read_text()))
            sha = None
        else:
            state, sha = remote_state()
        acknowledged = []
        if (not args.dry_run and args.notify_maintainer and
                config.get("maintainer_notifications") == "issues"):
            new_items = [item for item in findings if item["key"] not in state["seen_keys"]]
            if new_items:
                issues = all_watch_issues()
                for item in new_items:
                    try:
                        result = notify_issue(item, instructions(item, report), issues)
                        report["notifications"].append({"key": item["key"], **result})
                        acknowledged.append(item["key"])
                    except Exception as error:
                        report["errors"].append({"probe": "maintainer_notification", "error": type(error).__name__})
        report["human_notifications_sent"] = any(
            r.get("status") == "bot_issue_created_email_receipt_unverified" for r in report["notifications"])
        if report["errors"]:
            report["status"] = "DEGRADED"
        next_state, events = advance_state(state, findings, report, acknowledged)
        report["events"] = events
        report["deduplication"] = {"seen_before": len(state["seen_keys"]),
                                   "seen_after": len(next_state["seen_keys"]),
                                   "state_path": STATE_PATH,
                                   "write_requested": not args.dry_run}
        if not args.dry_run:
            persist_state(next_state, sha)
            report["deduplication"]["committed"] = True
    except Exception as error:
        report["errors"].append({"probe": "public_state", "error": type(error).__name__})
    if report["errors"]:
        report["status"] = "DEGRADED"
    report["dry_run"] = args.dry_run
    write_report(report, args.report)
    log("状态：" + report["status"])
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
