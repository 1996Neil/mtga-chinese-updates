import Foundation
import CryptoKit
import Darwin
import zlib

let clientVersion = 4
struct Problem: Error, CustomStringConvertible { let description: String }
func need(_ condition: Bool, _ message: String) throws { if !condition { throw Problem(description: message) } }
let fm = FileManager.default
let encoder: JSONEncoder = { let e = JSONEncoder(); e.outputFormatting = [.sortedKeys, .prettyPrinted]; return e }()
func json<T: Decodable>(_ type: T.Type, _ data: Data) throws -> T { try JSONDecoder().decode(type, from: data) }
struct Fingerprint: Codable, Equatable { let sha256: String; let bytes: Int64 }
struct Envelope: Codable { let payload: String; let signature: String }
struct Artifact: Codable { let name: String; let fingerprint: Fingerprint; let url: String? }
struct ReleaseRef: Codable { let id: String; let revision: Int64; let client_version: String; let steam_build: String; let manifest: Artifact }
struct Index: Codable { let schema: String; let sequence: Int64; let expires_at: String; let channel: String; let releases: [ReleaseRef] }
struct Resource: Codable { let layer: String; let relative: String; let input: Fingerprint; let output: Fingerprint; let patch: Artifact; let full: Artifact? }
struct Protected: Codable { let relative: String; let fingerprint: Fingerprint }
struct Acceptance: Codable { let status: String; let evidence_sha256: String?; let scope: String }
struct Release: Codable {
    let schema: String; let id: String; let revision: Int64; let client_version: String; let steam_build: String
    let minimum_client: Int; let platform: String; let architectures: [String]; let acceptance: Acceptance
    let resources: [Resource]; let protected_files: [Protected]
}
struct Profile: Codable { let schema: String; let feed: String; let public_key: String; let channel: String }
struct Row: Codable { let layer: String; let relative: String; let before: Fingerprint; let after: Fingerprint; let original: Fingerprint; let original_available: Bool? }
struct Journal: Codable {
    let schema: String; let id: String; let release_id: String; let revision: Int64; let game: String
    let client_version: String; let steam_build: String; let rows: [Row]; let protected_files: [Protected]
    var state: String
}
struct Seen: Codable { let sequence: Int64; let fingerprint: Fingerprint; let checked_at: Double }
struct Game { let root: URL; let version: String; let build: String }

func matching(_ value: String, _ pattern: String) -> Bool { value.range(of: pattern, options: .regularExpression) != nil }
func basename(_ name: String) throws { try need(matching(name, "^[A-Za-z0-9][A-Za-z0-9._-]{0,150}$") && name != "." && name != "..", "更新包文件名无效。") }
func validFP(_ fp: Fingerprint) throws { try need(matching(fp.sha256, "^[0-9a-f]{64}$") && fp.bytes >= 0 && fp.bytes <= 1073741824, "文件哈希或大小无效。") }
func checked(_ url: URL, regular: Bool = true) throws {
    let u = url.standardizedFileURL
    try need(u.resolvingSymlinksInPath().path == u.path, "拒绝符号链接路径：\(u.path)")
    var st = stat(); try need(lstat(u.path, &st) == 0, "找不到文件：\(u.path)")
    if regular { try need((st.st_mode & S_IFMT) == S_IFREG, "目标不是普通文件：\(u.path)") }
}
func folder(_ url: URL) throws {
    try need(url.standardizedFileURL.resolvingSymlinksInPath().path == url.standardizedFileURL.path, "拒绝符号链接目录。")
    try fm.createDirectory(at: url, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    try checked(url, regular: false)
    var st = stat(); try need(lstat(url.path, &st) == 0 && (st.st_mode & S_IFMT) == S_IFDIR, "状态路径不是目录。")
}
func fingerprint(_ url: URL) throws -> Fingerprint {
    try checked(url)
    let h = try FileHandle(forReadingFrom: url); defer { try? h.close() }
    var digest = SHA256(); var count: Int64 = 0
    while let data = try h.read(upToCount: 1024 * 1024), !data.isEmpty { count += Int64(data.count); try need(count <= 1073741824, "文件超过允许大小。") ; digest.update(data: data) }
    return Fingerprint(sha256: digest.finalize().map { String(format: "%02x", $0) }.joined(), bytes: count)
}
func verify(_ url: URL, _ fp: Fingerprint) throws { try validFP(fp); try need(try fingerprint(url) == fp, "文件完整性不匹配：\(url.lastPathComponent)") }
func small(_ url: URL, limit: Int = 4 * 1024 * 1024) throws -> Data { try checked(url); let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? Int.max; try need(size <= limit, "文件过大：\(url.lastPathComponent)"); return try Data(contentsOf: url) }
func syncDirectory(_ url: URL) throws {
    let fd = open(url.path, O_RDONLY); try need(fd >= 0, "无法打开目录以确认持久化。")
    defer { close(fd) }
    try need(fsync(fd) == 0, "目录同步失败，已停止后续写入。")
}
func atomic(_ data: Data, _ url: URL, mode: mode_t = 0o600) throws {
    try folder(url.deletingLastPathComponent())
    if fm.fileExists(atPath: url.path) { try checked(url) }
    let tmp = url.deletingLastPathComponent().appendingPathComponent(".mtga-" + UUID().uuidString)
    defer { try? fm.removeItem(at: tmp) }
    let fd = open(tmp.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, mode)
    try need(fd >= 0, "无法创建临时文件。")
    let h = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    try h.write(contentsOf: data); try h.synchronize(); try h.close()
    try need(rename(tmp.path, url.path) == 0, "无法原子替换文件。")
    #if TESTING
    if ProcessInfo.processInfo.environment["MTGA_TEST_FAIL_JOURNAL_SYNC"] == "1" && url.lastPathComponent == "installation.json" { throw Problem(description: "模拟安装记录目录同步失败。") }
    #endif
    try syncDirectory(url.deletingLastPathComponent())
}
func save<T: Encodable>(_ value: T, _ url: URL) throws { try atomic(encoder.encode(value), url) }

func setupProfile(_ profile: Profile, state: URL) throws {
    let profilePath = state.appendingPathComponent("profile.json")
    let trustPath = state.appendingPathComponent("trust.json")
    let paths = [profilePath, trustPath].filter { fm.fileExists(atPath: $0.path) }
    let existing = try paths.map { try json(Profile.self, small($0)) }
    for old in existing {
        try need(old.schema == profile.schema && old.public_key == profile.public_key && old.feed == profile.feed,
                 "安装包的更新地址或公钥与现有安装不同，已保留原配置和备份。")
        try need(old.channel == profile.channel || (old.channel == "testing" && profile.channel == "stable"),
                 "不支持自动切换此订阅渠道；已保留原配置和备份。")
    }
    let promoting = existing.contains { $0.channel != profile.channel }
    let receiptPath = state.appendingPathComponent("channel-promotion.json")
    // A saved intent makes an interrupted trust/profile pair update retryable.
    // Neither profile may change feed or key, even during recovery.
    let recovering: Bool
    if fm.fileExists(atPath: receiptPath.path) {
        let saved = try json(Profile.self, small(receiptPath))
        recovering = saved.schema == profile.schema && saved.feed == profile.feed &&
            saved.public_key == profile.public_key && saved.channel == "stable" && profile.channel == "stable"
    } else { recovering = false }
    if promoting || recovering {
        let journalPath = state.appendingPathComponent("installation.json")
        if fm.fileExists(atPath: journalPath.path) {
            let journal = try json(Journal.self, small(journalPath))
            try need(["installed", "rolled-back"].contains(journal.state),
                     "上次安装或恢复尚未完成，请先用原工具恢复，再切换正式渠道。")
        }
        let scratch = state.appendingPathComponent(".channel-check-" + UUID().uuidString)
        try folder(scratch); defer { try? fm.removeItem(at: scratch) }
        let indexPath = scratch.appendingPathComponent("index-stable.json")
        if profile.feed.hasPrefix("https://") {
            guard let url = URLComponents(string: profile.feed) else { throw Problem(description: "更新地址无效。") }
            try need(url.scheme == "https" && url.host != nil && url.user == nil && url.password == nil &&
                     url.query == nil && url.fragment == nil, "更新地址必须为不含凭据的 HTTPS 目录。")
            let result = try run("/usr/bin/curl", ["--disable", "--fail", "--silent", "--show-error",
                "--location", "--max-redirs", "5", "--proto", "=https", "--proto-redir", "=https",
                "--connect-timeout", "15", "--max-time", "60", "--max-filesize", "2097152",
                "--output", indexPath.path, profile.feed + "/index-stable.json"])
            try need(result.0 == 0, "正式渠道索引暂时无法下载，原配置和备份已保留；请稍后重试。")
        } else {
            #if TESTING
            try need(profile.feed.hasPrefix("/"), "测试更新源路径无效。")
            try fm.copyItem(at: URL(fileURLWithPath: profile.feed).appendingPathComponent("index-stable.json"), to: indexPath)
            #else
            throw Problem(description: "渠道迁移仅支持 HTTPS 更新源。")
            #endif
        }
        let data = try small(indexPath, limit: 2 * 1024 * 1024)
        let envelope = try json(Envelope.self, data)
        guard let payload = Data(base64Encoded: envelope.payload),
              let signature = Data(base64Encoded: envelope.signature),
              let publicData = Data(base64Encoded: profile.public_key) else {
            throw Problem(description: "正式渠道签名封装无效，未切换配置。")
        }
        let key = try Curve25519.Signing.PublicKey(rawRepresentation: publicData)
        try need(key.isValidSignature(signature, for: payload), "正式渠道签名校验失败，未切换配置。")
        let index = try json(Index.self, payload)
        try need(index.schema == "MTGA_CN_INDEX_V1" && index.channel == "stable" &&
                 index.sequence > 0 && !index.releases.isEmpty && index.releases.count <= 10000,
                 "正式渠道索引无效，未切换配置。")
        guard let expiry = ISO8601DateFormatter().date(from: index.expires_at) else {
            throw Problem(description: "正式渠道有效期无效。")
        }
        try need(expiry > Date(), "正式渠道索引已过期，未切换配置。")
        let seenPath = state.appendingPathComponent("seen-stable.json")
        if fm.fileExists(atPath: seenPath.path) {
            let seen = try json(Seen.self, small(seenPath))
            let indexFP = try fingerprint(indexPath)
            try need(index.sequence >= seen.sequence &&
                     (index.sequence != seen.sequence || indexFP == seen.fingerprint),
                     "正式渠道索引倒退或同序号内容不同，未切换配置。")
            try need(Date().timeIntervalSince1970 >= seen.checked_at - 300, "本机时间倒退，未切换配置。")
        }
        for path in paths {
            let backup = state.appendingPathComponent(path.lastPathComponent + ".before-stable")
            if !fm.fileExists(atPath: backup.path) { try atomic(small(path), backup) }
        }
        try save(profile, receiptPath)
        try save(profile, trustPath)
        #if TESTING
        if ProcessInfo.processInfo.environment["MTGA_TEST_INTERRUPT_CHANNEL_PROMOTION"] == "1" {
            throw Problem(description: "模拟渠道切换中断。")
        }
        #endif
    }
    try save(profile, profilePath)
    if promoting || recovering {
        try fm.removeItem(at: receiptPath)
        try syncDirectory(state)
    }
}
func copyVerified(_ source: URL, _ target: URL, _ fp: Fingerprint, precommit: () throws -> Void = {}) throws {
    try verify(source, fp); try folder(target.deletingLastPathComponent())
    if fm.fileExists(atPath: target.path) { try checked(target) }
    let tmp = target.deletingLastPathComponent().appendingPathComponent(".mtga-stage-" + UUID().uuidString)
    defer { try? fm.removeItem(at: tmp) }
    #if TESTING
    if ProcessInfo.processInfo.environment["MTGA_TEST_ENOSPC_TARGET"] == target.lastPathComponent { throw Problem(description: "模拟目标磁盘空间不足 ENOSPC。") }
    #endif
    try fm.copyItem(at: source, to: tmp); try verify(tmp, fp)
    if fm.fileExists(atPath: target.path) {
        let permissions = try fm.attributesOfItem(atPath: target.path)[.posixPermissions] ?? 0o600
        try fm.setAttributes([.posixPermissions: permissions], ofItemAtPath: tmp.path)
    }
    let h = try FileHandle(forWritingTo: tmp); try h.synchronize(); try h.close()
    try precommit()
    try need(rename(tmp.path, target.path) == 0, "无法替换资源：\(target.lastPathComponent)")
    try syncDirectory(target.deletingLastPathComponent())
    try verify(target, fp)
}
func run(_ path: String, _ args: [String], input: FileHandle? = nil) throws -> (Int32, String) {
    let p = Process(); p.executableURL = URL(fileURLWithPath: path); p.arguments = args
    let out = Pipe(); p.standardOutput = out; p.standardError = out
    if let input = input { p.standardInput = input }
    try p.run(); let data = out.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
    return (p.terminationStatus, String(decoding: data, as: UTF8.self))
}
func stopped() throws {
    #if TESTING
    if ProcessInfo.processInfo.environment["MTGA_TEST_ASSUME_STOPPED"] == "1" { return }
    #endif
    let r = try run("/usr/bin/pgrep", ["-x", "MTGA"])
    try need(r.0 == 1 && r.1.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, "请完全退出 MTGA 后再更新或恢复。")
}
func vdfValue(_ text: String, _ key: String) -> String? {
    let regex = try! NSRegularExpression(pattern: "\"" + NSRegularExpression.escapedPattern(for: key) + "\"\\s*\"([^\"]*)\"")
    guard let m = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)), let r = Range(m.range(at: 1), in: text) else { return nil }; return String(text[r])
}
func inspectGame(_ library: URL) throws -> Game {
    let apps = library.appendingPathComponent("steamapps")
    let text = String(decoding: try small(apps.appendingPathComponent("appmanifest_2141910.acf")), as: UTF8.self)
    try need(vdfValue(text, "appid") == "2141910", "Steam 游戏记录不匹配。")
    try need(vdfValue(text, "StateFlags") == "4", "Steam 更新尚未完成，请等待下载和验证结束。")
    guard let build = vdfValue(text, "buildid"), let dir = vdfValue(text, "installdir") else { throw Problem(description: "Steam 记录不完整。") }
    try need(matching(build, "^[0-9]+$") && !dir.contains("/") && dir != "." && dir != ".." && !dir.isEmpty, "Steam 安装路径或版本无效。")
    let root = apps.appendingPathComponent("common").appendingPathComponent(dir).standardizedFileURL
    try checked(root, regular: false)
    let info = try PropertyListSerialization.propertyList(from: small(root.appendingPathComponent("MTGA.app/Contents/Info.plist")), options: [], format: nil) as? [String: Any]
    guard let version = info?["CFBundleShortVersionString"] as? String else { throw Problem(description: "无法识别 MTGA 版本。") }
    return Game(root: root, version: version, build: build)
}
func discover(_ explicit: String?) throws -> (URL, Game) {
    if let path = explicit { let l = URL(fileURLWithPath: path).standardizedFileURL.resolvingSymlinksInPath(); return (l, try inspectGame(l)) }
    let steam = fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Steam")
    var libraries = [steam]
    let list = steam.appendingPathComponent("steamapps/libraryfolders.vdf")
    if fm.fileExists(atPath: list.path) {
        let t = String(decoding: try small(list), as: UTF8.self)
        let regex = try NSRegularExpression(pattern: "\"path\"\\s*\"([^\"]+)\"")
        for m in regex.matches(in: t, range: NSRange(t.startIndex..., in: t)) { if let r = Range(m.range(at: 1), in: t) { libraries.append(URL(fileURLWithPath: String(t[r]).replacingOccurrences(of: "\\\\", with: "\\"))) } }
    }
    var matches: [(URL, Game)] = []; var seen = Set<String>()
    for lib in libraries {
        let l = lib.standardizedFileURL.resolvingSymlinksInPath()
        if seen.insert(l.path).inserted && fm.fileExists(atPath: l.appendingPathComponent("steamapps/appmanifest_2141910.acf").path) { matches.append((l, try inspectGame(l))) }
    }
    try need(matches.count == 1, "未找到唯一的 Steam MTGA 安装。可用 --steam-library 指定包含 steamapps 的目录。")
    return matches[0]
}
func target(_ root: URL, _ relative: String) throws -> URL {
    try need(!relative.hasPrefix("/") && !relative.split(separator: "/", omittingEmptySubsequences: false).contains(where: { $0 == ".." || $0 == "." || $0.isEmpty }) && !relative.contains("\\"), "拒绝不安全资源路径。")
    let u = root.appendingPathComponent(relative).standardizedFileURL
    try need(u.path.hasPrefix(root.path + "/"), "资源路径越界。")
    try checked(u)
    return u
}
func validResource(_ layer: String, _ rel: String) -> Bool {
    switch layer {
    case "card": return matching(rel, "^MTGA_Data/Downloads/Raw/Raw_CardDatabase_[0-9a-f]{32}\\.mtga$")
    case "client": return matching(rel, "^MTGA_Data/Downloads/Raw/Raw_ClientLocalization_[0-9a-f]{32}\\.mtga$")
    case "resources": return rel == "MTGA.app/Contents/Resources/Data/resources.assets"
    case "bootstrap_fonts": return rel == "MTGA.app/Contents/Resources/Data/sharedassets0.assets"
    case "font_bundle": return matching(rel, "^MTGA_Data/Downloads/AssetBundle/Fonts_[0-9a-f-]+\\.mtga$")
    default: return false
    }
}
// Signed external patches may only use the same distribution repository's Releases.
func releaseURL(_ value: String, feed: String, name: String) throws -> URL {
    try basename(name)
    guard let source = URLComponents(string: feed), let remote = URLComponents(string: value), let result = remote.url else { throw Problem(description: "下载地址格式无效。") }
    let sourceParts = source.path.split(separator: "/").map(String.init)
    let parts = remote.path.split(separator: "/").map(String.init)
    try need(source.scheme == "https" && source.host == "raw.githubusercontent.com" && source.port == nil && source.user == nil && source.password == nil && source.query == nil && source.fragment == nil && sourceParts.count >= 3, "外部资源下载需要固定 GitHub 分发源。")
    try need(parts.count == 6 && parts[0] == sourceParts[0] && parts[1] == sourceParts[1] && parts[2] == "releases" && parts[3] == "download" && parts[5] == name, "差分下载地址不属于订阅仓库。")
    try basename(parts[4])
    try need(remote.scheme == "https" && remote.host == "github.com" && remote.port == nil && remote.user == nil && remote.password == nil && remote.query == nil && remote.fragment == nil && value == "https://github.com/" + parts.joined(separator: "/"), "拒绝不安全的差分下载地址。")
    return result
}
func protectedPath(_ rel: String) -> Bool {
    return ["MTGA.app/Contents/Info.plist", "MTGA.app/Contents/MacOS/MTGA", "MTGA.app/Contents/Frameworks/GameAssembly.dylib"].contains(rel)
      || matching(rel, "^MTGA_Data/Downloads/Manifest_[A-Za-z0-9_]+\\.mtga$")
      || matching(rel, "^MTGA_Data/Downloads/AssetBundle/Bucket_Card\\.(FieldFont|FontMaterialSettings)_[A-Za-z0-9_-]+\\.mtga$")
}
func validateRelease(_ release: Release, _ ref: ReleaseRef, _ game: Game, _ channel: String) throws {
    try need(release.schema == "MTGA_CN_RELEASE_V1" && release.id == ref.id && release.revision == ref.revision && release.revision > 0, "发布清单身份不匹配。")
    try need(release.client_version == ref.client_version && release.steam_build == ref.steam_build && release.client_version == game.version && release.steam_build == game.build, "发布包不适用于当前游戏版本。")
    try need(release.minimum_client <= clientVersion && release.minimum_client > 0, "需要升级维护工具。请从 https://github.com/1996Neil/mtga-chinese-updates 重新下载安装包，双击「安装 MTGA 中文维护.command」。原有配置和备份会保留。")
    #if arch(arm64)
    let arch = "arm64"
    #else
    let arch = "x86_64"
    #endif
    try need(release.platform == "macOS-Steam" && release.architectures.contains(arch), "发布包不支持当前平台或处理器。")
    try need(["accepted", "testing"].contains(release.acceptance.status), "发布验收状态无效。")
    if channel == "stable" { try need(release.acceptance.status == "accepted" && matching(release.acceptance.evidence_sha256 ?? "", "^[0-9a-f]{64}$") && !release.acceptance.scope.isEmpty, "此包尚未完成发布者验收，不能通过稳定渠道安装。") }
    try need(release.resources.count == 5 && Set(release.resources.map(\.layer)) == Set(["card", "client", "resources", "bootstrap_fonts", "font_bundle"]), "资源层集合无效。")
    for row in release.resources { try need(validResource(row.layer, row.relative), "发布包尝试写入禁止的文件。") ; try validFP(row.input); try validFP(row.output); try validFP(row.patch.fingerprint); try basename(row.patch.name) }
    for row in release.resources {
        if let full = row.full {
            try basename(full.name); try validFP(full.fingerprint)
            try need(full.name.hasSuffix(".full") && full.fingerprint == row.output, "完整资源必须与签名输出一致。")
        }
    }
    let paths = release.protected_files.map(\.relative)
    try need(Set(paths).count == paths.count && Set(["MTGA.app/Contents/Info.plist", "MTGA.app/Contents/MacOS/MTGA", "MTGA.app/Contents/Frameworks/GameAssembly.dylib"]).isSubset(of: Set(paths)) && paths.contains(where: { $0.hasPrefix("MTGA_Data/Downloads/Manifest_") }), "发布包缺少受保护文件校验。")
    for row in release.protected_files { try need(protectedPath(row.relative), "受保护路径无效。") ; try validFP(row.fingerprint) }
}
func activeResources(_ game: URL, _ rows: [Resource]) throws {
    let labels = ["card": "卡牌文本", "client": "客户端文本", "font_bundle": "字体"]
    for row in rows where ["card", "client", "font_bundle"].contains(row.layer) {
        try need(validResource(row.layer, row.relative), "发布包尝试使用禁止的资源路径。")
        let expected = game.appendingPathComponent(row.relative).standardizedFileURL
        let directory = expected.deletingLastPathComponent()
        try checked(directory, regular: false)
        let prefix = row.layer == "card" ? "Raw_CardDatabase_" : (row.layer == "client" ? "Raw_ClientLocalization_" : "Fonts_")
        let names = try fm.contentsOfDirectory(atPath: directory.path).filter { $0.hasPrefix(prefix) && $0.hasSuffix(".mtga") }.sorted()
        let label = labels[row.layer] ?? row.layer
        try need(!names.isEmpty, "游戏的\(label)资源尚未就绪。请先启动游戏完成资源下载，退出游戏后再检查；尚未修改游戏文件。")
        try need(names.count == 1, "发现多份活动资源，拒绝猜测：\(label)。请保留当前文件并联系维护者；尚未修改游戏文件。")
        let relative = row.relative.split(separator: "/").dropLast().joined(separator: "/") + "/" + names[0]
        try need(validResource(row.layer, relative), "活动资源名称格式无法识别：\(label)。尚未修改游戏文件。")
        _ = try target(game, relative)
        try need(names[0] == expected.lastPathComponent,
                 "检测到游戏资源已更新，当前汉化包尚未匹配这一资源世代。\n资源：\(label)\n当前文件：\(names[0])\n汉化包对应：\(expected.lastPathComponent)\n请等待维护者提供匹配版本；无需重新安装游戏，也不要把旧文件改名覆盖。尚未修改游戏文件。")
    }
}
func verifyProtected(_ game: URL, _ rows: [Protected]) throws { for row in rows { try need(protectedPath(row.relative), "校验路径无效。") ; try verify(target(game, row.relative), row.fingerprint) } }

// Data-only delta: magic, output length, op count; each copy or zlib literal is bounded.
func applyDelta(_ base: URL, _ patch: URL, _ output: URL, _ expected: Fingerprint) throws {
    let bytes = try small(patch, limit: 512 * 1024 * 1024); var cursor = 0
    func take(_ n: Int) throws -> Data { try need(n >= 0 && cursor <= bytes.count - n, "差分包截断。") ; let d = bytes.subdata(in: cursor..<cursor+n); cursor += n; return d }
    func integer(_ n: Int) throws -> UInt64 { let data = try take(n); return data.enumerated().reduce(UInt64(0)) { $0 | UInt64($1.element) << (8 * $1.offset) } }
    try need(try take(8) == Data("MTGADL01".utf8), "差分格式不受支持。")
    let size = try integer(8), count = try integer(4)
    try need(size == UInt64(expected.bytes) && count <= 1000000, "差分输出大小或操作数无效。")
    try checked(base); let source = try FileHandle(forReadingFrom: base); defer { try? source.close() }
    let sourceSize = try fingerprint(base).bytes
    try need(!fm.fileExists(atPath: output.path), "拒绝覆盖暂存文件。")
    let fd = open(output.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600); try need(fd >= 0, "无法建立候选文件。")
    let dest = FileHandle(fileDescriptor: fd, closeOnDealloc: true); defer { try? dest.close() }
    var produced: UInt64 = 0
    for _ in 0..<count {
        let kind = try integer(1)
        if kind == 0 {
            let offset = try integer(8), length = try integer(4)
            try need(length > 0 && offset <= UInt64(sourceSize) && length <= UInt64(sourceSize) - offset && produced <= size && length <= size - produced, "差分复制范围越界。")
            try source.seek(toOffset: offset); var left = length
            while left > 0 { let data = try source.read(upToCount: Int(min(left, 1024*1024))) ?? Data(); try need(!data.isEmpty, "底稿读取中断。") ; try dest.write(contentsOf: data); left -= UInt64(data.count) }
            produced += length
        } else if kind == 1 {
            let length = try integer(4), packed = try integer(4)
            try need(length > 0 && length <= 4194304 && packed > 0 && packed <= 8388608 && produced <= size && length <= size - produced, "差分解压范围无效。")
            let input = try take(Int(packed)); var out = Data(count: Int(length)); var outLength = uLongf(length)
            var inputLength = uLong(input.count)
            let status: Int32 = out.withUnsafeMutableBytes { dst in input.withUnsafeBytes { src in uncompress2(dst.bindMemory(to: Bytef.self).baseAddress, &outLength, src.bindMemory(to: Bytef.self).baseAddress, &inputLength) } }
            try need(status == Z_OK && outLength == length && inputLength == input.count, "差分解压失败。") ; try dest.write(contentsOf: out); produced += length
        } else { throw Problem(description: "差分包包含未知指令。") }
    }
    try need(produced == size && cursor == bytes.count, "差分包长度不匹配或包含多余数据。")
    try dest.synchronize(); try dest.close(); try verify(output, expected)
}

struct IncompatibleDownloadRange: Error, CustomStringConvertible {
    let description: String
}

final class Client {
    let state: URL; let profile: Profile; let library: URL; let game: Game
    var lockFD: Int32 = -1
    var gameLockFD: Int32 = -1
    var transient: [URL] = []
    init(state: URL, profile: Profile, library: URL, game: Game) throws {
        self.state = state; self.profile = profile; self.library = library; self.game = game
        try need(profile.schema == "MTGA_CN_PROFILE_V1" && ["stable", "testing"].contains(profile.channel), "更新源配置无效。")
        guard let key = Data(base64Encoded: profile.public_key) else { throw Problem(description: "更新源公钥无效。") }; _ = try Curve25519.Signing.PublicKey(rawRepresentation: key)
        try folder(state)
        let path = state.appendingPathComponent("client.lock"); if fm.fileExists(atPath: path.path) { try checked(path) }
        lockFD = open(path.path, O_RDWR | O_CREAT | O_NOFOLLOW, 0o600)
        try need(lockFD >= 0 && flock(lockFD, LOCK_EX | LOCK_NB) == 0, "另一项维护操作正在运行，请稍后重试。")
        // All state directories for this user share one canonical-game lock.
        let locks = fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/MTGAChineseSubscriberLocks")
        try folder(locks)
        var gameStat = stat(); try need(stat(game.root.path, &gameStat) == 0, "无法确定游戏目录身份。")
        let gameKey = "\(gameStat.st_dev)-\(gameStat.st_ino)"
        let gameLock = locks.appendingPathComponent(gameKey + ".lock")
        if fm.fileExists(atPath: gameLock.path) { try checked(gameLock) }
        gameLockFD = open(gameLock.path, O_RDWR | O_CREAT | O_NOFOLLOW, 0o600)
        try need(gameLockFD >= 0 && flock(gameLockFD, LOCK_EX | LOCK_NB) == 0, "另一状态目录正在维护此游戏，请稍后重试。")
        let trustPath = state.appendingPathComponent("trust.json")
        if fm.fileExists(atPath: trustPath.path) {
            let old = try json(Profile.self, small(trustPath))
            try need(old.public_key == profile.public_key && old.feed == profile.feed && old.channel == profile.channel, "此状态目录已绑定不同的更新源，请使用独立状态目录。")
        } else { try save(profile, trustPath) }
    }
    deinit { if gameLockFD >= 0 { flock(gameLockFD, LOCK_UN); close(gameLockFD) }; for file in transient { try? fm.removeItem(at: file) }; if lockFD >= 0 { flock(lockFD, LOCK_UN); close(lockFD) } }
    var journalURL: URL { state.appendingPathComponent("installation.json") }
    func journal() throws -> Journal? {
        guard fm.fileExists(atPath: journalURL.path) else { return nil }
        let j = try json(Journal.self, small(journalURL))
        try need(j.schema == "MTGA_CN_SUBSCRIBER_TX_V1" && matching(j.id, "^[A-Fa-f0-9-]{36}$") && j.game == game.root.path && ["prepared", "installing", "installed", "rolling-back", "rolled-back"].contains(j.state), "安装记录无效或属于另一游戏目录。")
        try need(j.rows.count == 5 && Set(j.rows.map(\.layer)).count == 5, "安装记录资源不完整。")
        for r in j.rows { try need(validResource(r.layer, r.relative), "安装记录路径无效。") ; try validFP(r.before); try validFP(r.after); try validFP(r.original) }
        return j
    }
    func txdir(_ j: Journal) -> URL { state.appendingPathComponent("transactions").appendingPathComponent(j.id) }
    func backup(_ j: Journal, _ row: Row) -> URL { txdir(j).appendingPathComponent("before-" + row.layer) }
    func original(_ j: Journal, _ row: Row) -> URL { txdir(j).appendingPathComponent("original-" + row.layer) }
    func cacheRegular(_ url: URL) throws -> Int64 {
        try checked(url)
        var st = stat(); try need(lstat(url.path, &st) == 0 && st.st_nlink == 1, "下载缓存不能是硬链接。")
        return st.st_size
    }
    func pruneCache(_ dir: URL, keep: Set<String>, reserve: Int64) throws {
        var budget: Int64 = 2 * 1024 * 1024 * 1024
        #if TESTING
        if let raw = ProcessInfo.processInfo.environment["MTGA_TEST_CACHE_BUDGET"], let n = Int64(raw) { budget = n }
        #endif
        try need(reserve <= budget, "下载缓存预算不足。")
        var rows: [(URL, Int64, Date)] = []; var used: Int64 = 0
        for file in try fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.contentModificationDateKey]) {
            try need(matching(file.lastPathComponent, "^[a-f0-9]{64}-[0-9]+(?:\\.[A-Fa-f0-9-]{36})?\\.(?:delta|partial|incoming|headers)$"), "下载缓存目录包含未知文件，已停止清理。")
            let size = try cacheRegular(file)
            if !keep.contains(file.lastPathComponent) { rows.append((file, size, try file.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate ?? .distantPast)); used += size }
        }
        for row in rows.sorted(by: { $0.2 < $1.2 }) where used + reserve > budget {
            try fm.removeItem(at: row.0); used -= row.1
        }
        try need(used + reserve <= budget, "下载缓存空间不足。")
    }
    func createCacheFile(_ url: URL) throws {
        let fd = open(url.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        try need(fd >= 0, "无法安全创建下载缓存。")
        close(fd)
    }
    func responseStatus(_ headers: URL, offset: Int64, total: Int64) throws -> Int {
        let text = String(decoding: try small(headers, limit: 65536), as: UTF8.self).replacingOccurrences(of: "\r\n", with: "\n")
        guard let block = text.components(separatedBy: "\n\n").last(where: { $0.hasPrefix("HTTP/") }) else { throw Problem(description: "下载响应头缺失。") }
        let lines = block.components(separatedBy: "\n")
        let first = lines[0].split(separator: " ")
        guard first.count >= 2, let status = Int(first[1]) else { throw Problem(description: "下载响应状态无效。") }
        if offset > 0 && status != 206 { return status }
        try need(status == 200 || status == 206, "下载响应状态无效：\(status)。")
        let fields = lines.dropFirst().compactMap { line -> (String, String)? in
            guard let colon = line.firstIndex(of: ":") else { return nil }
            return (String(line[..<colon]).lowercased(), String(line[line.index(after: colon)...]).trimmingCharacters(in: .whitespaces))
        }
        if status == 206 {
            let ranges = fields.filter { $0.0 == "content-range" }.map { $0.1 }
            guard ranges == ["bytes \(offset)-\(total - 1)/\(total)"] else { throw IncompatibleDownloadRange(description: "下载续传范围不匹配。") }
        }
        for value in fields.filter({ $0.0 == "content-length" }).map({ $0.1 }) {
            guard Int64(value) == total - offset else { throw IncompatibleDownloadRange(description: "下载响应长度不匹配。") }
        }
        return status
    }
    func deltaDownload(_ name: String, url: URL, fp: Fingerprint, limit: Int) throws -> URL {
        try validFP(fp); try need(fp.bytes > 0 && fp.bytes <= Int64(limit), "差分大小无效。")
        let dir = state.appendingPathComponent("patch-cache"); try folder(dir)
        let stem = fp.sha256 + "-" + String(fp.bytes)
        let complete = dir.appendingPathComponent(stem + ".delta"), partial = dir.appendingPathComponent(stem + ".partial")
        if fm.fileExists(atPath: complete.path) {
            _ = try cacheRegular(complete)
            if try fingerprint(complete) == fp {
                try fm.setAttributes([.modificationDate: Date()], ofItemAtPath: complete.path)
                print("已复核下载缓存：\(name)"); fflush(stdout); return complete
            }
            try fm.removeItem(at: complete)
        }
        if fm.fileExists(atPath: partial.path) {
            let size = try cacheRegular(partial)
            if size == fp.bytes {
                if try fingerprint(partial) == fp { try fm.moveItem(at: partial, to: complete); return complete }
                try fm.removeItem(at: partial)
            } else if size > fp.bytes { try fm.removeItem(at: partial) }
        }
        try pruneCache(dir, keep: [partial.lastPathComponent], reserve: fp.bytes * 2 + 65536)
        if !fm.fileExists(atPath: partial.path) { try createCacheFile(partial) }
        // The curl child inherits client.lock as stdin. If this parent is killed,
        // the child keeps that lock until it exits; a new client cannot race it.
        let inheritedLock = FileHandle(fileDescriptor: lockFD, closeOnDealloc: false)
        for attempt in 0..<2 {
            let offset = try cacheRegular(partial)
            let token = UUID().uuidString
            let incoming = dir.appendingPathComponent(stem + "." + token + ".incoming")
            let headers = dir.appendingPathComponent(stem + "." + token + ".headers")
            try createCacheFile(incoming); try createCacheFile(headers)
            defer { try? fm.removeItem(at: incoming); try? fm.removeItem(at: headers) }
            print("\(offset > 0 ? "继续下载" : "正在下载")：\(name)（\(String(format: "%.1f", Double(offset) / 1048576))/\(String(format: "%.1f", Double(fp.bytes) / 1048576)) MiB）。完成完整校验前不修改游戏。")
            fflush(stdout)
            var args = ["--disable", "--fail", "--silent", "--show-error", "--retry", "0", "--speed-limit", "1024", "--speed-time", "90", "--location", "--max-redirs", "3", "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "10", "--max-time", "3600", "--max-filesize", String(fp.bytes - offset), "--dump-header", headers.path, "--output", incoming.path]
            if offset > 0 { args += ["--range", "\(offset)-"] }
            args.append(url.absoluteString)
            var curl = "/usr/bin/curl"
            #if TESTING
            curl = ProcessInfo.processInfo.environment["MTGA_TEST_CURL"] ?? curl
            #endif
            let r = try run(curl, args, input: inheritedLock)
            let received = try cacheRegular(incoming)
            var response = 0
            do { response = try responseStatus(headers, offset: offset, total: fp.bytes) }
            catch {
                if offset > 0 && attempt == 0 && error is IncompatibleDownloadRange { try fm.removeItem(at: partial); try createCacheFile(partial); continue }
                throw error
            }
            if offset > 0 && response != 206 {
                if attempt == 0 && [200, 416].contains(response) { try fm.removeItem(at: partial); try createCacheFile(partial); continue }
                throw Problem(description: "续传响应无效：\(response)。已有断点保留。")
            }
            try need(received <= fp.bytes - offset, "下载响应超过签名大小。")
            if received > 0 {
                let source = try FileHandle(forReadingFrom: incoming); defer { try? source.close() }
                let dest = try FileHandle(forWritingTo: partial); defer { try? dest.close() }
                try dest.seekToEnd(); var left = received
                while left > 0 {
                    guard let bytes = try source.read(upToCount: Int(min(left, 1024 * 1024))), !bytes.isEmpty else { throw Problem(description: "下载临时文件截断。") }
                    try dest.write(contentsOf: bytes); left -= Int64(bytes.count)
                }
                try dest.synchronize(); try dest.close()
            }
            if try cacheRegular(partial) == fp.bytes {
                if try fingerprint(partial) != fp { try fm.removeItem(at: partial); throw Problem(description: "下载完整校验失败，已丢弃缓存。请重试完整下载。") }
                try fm.moveItem(at: partial, to: complete); try syncDirectory(dir); return complete
            }
            throw Problem(description: "下载未完成：\(name)（curl \(r.0)）。已保存断点，下次重试会继续；游戏文件未修改。")
        }
        throw Problem(description: "下载失败，游戏文件未修改。")
    }
    func fetch(_ name: String, expected: Fingerprint? = nil, limit: Int = 4*1024*1024, remote: String? = nil) throws -> URL {
        try basename(name)
        let cache = state.appendingPathComponent("downloads"); try folder(cache)
        let target = cache.appendingPathComponent(UUID().uuidString + "-" + name)
        transient.append(target)
        if profile.feed.hasPrefix("https://") {
            guard let base = URL(string: profile.feed), base.user == nil, base.password == nil, base.query == nil, base.fragment == nil else { throw Problem(description: "更新源地址必须是不含凭据或查询参数的 HTTPS 目录。") }
            let url = try remote.map { try releaseURL($0, feed: profile.feed, name: name) } ?? base.appendingPathComponent(name)
            let isPatch = name.hasSuffix(".delta") || name.hasSuffix(".full")
            if isPatch, let fp = expected { return try deltaDownload(name, url: url, fp: fp, limit: limit) }
            if isPatch { print("正在下载资源：\(name)（\((expected?.bytes ?? 0) / 1024 / 1024) MiB）。慢速网络可能需要较长时间。完成校验前不会修改游戏。"); fflush(stdout) }
            var curl = "/usr/bin/curl"
            #if TESTING
            curl = ProcessInfo.processInfo.environment["MTGA_TEST_CURL"] ?? curl
            #endif
            let r = try run(curl, ["--disable", "--fail", "--silent", "--show-error", "--retry", "2", "--retry-delay", "1", "--retry-max-time", "120", "--speed-limit", "1024", "--speed-time", "90", "--location", "--max-redirs", "3", "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "10", "--max-time", isPatch ? "1800" : "180", "--max-filesize", String(min(Int64(limit), expected?.bytes ?? Int64(limit))), "--output", target.path, url.absoluteString])
            try need(r.0 == 0, "下载失败：\(name)。请检查网络或更新源权限后重试。原有游戏文件未因下载而改变。")
        } else {
            try need(remote == nil, "本地源不能指定外部差分地址。")
            try need(profile.feed.hasPrefix("/"), "更新源仅支持 HTTPS 或明确选择的本地发布目录。")
            let source = URL(fileURLWithPath: profile.feed).appendingPathComponent(name); try checked(source)
            let attrs = try source.resourceValues(forKeys: [.fileSizeKey]); try need((attrs.fileSize ?? Int.max) <= limit, "更新文件超过大小限制。")
            try fm.copyItem(at: source, to: target)
        }
        try checked(target)
        let size = try target.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? Int.max; try need(size <= limit, "下载内容超过大小限制。")
        if let fp = expected { try verify(target, fp) }
        return target
    }
    func signed<T: Decodable>(_ type: T.Type, _ path: URL) throws -> T {
        let envelope = try json(Envelope.self, small(path))
        guard let payload = Data(base64Encoded: envelope.payload), let signature = Data(base64Encoded: envelope.signature), let keydata = Data(base64Encoded: profile.public_key) else { throw Problem(description: "签名封装无效。") }
        let key = try Curve25519.Signing.PublicKey(rawRepresentation: keydata)
        try need(key.isValidSignature(signature, for: payload), "更新签名校验失败，已拒绝使用。")
        return try json(type, payload)
    }
    func selected() throws -> Release? {
        let indexPath = try fetch("index-" + profile.channel + ".json")
        let indexFP = try fingerprint(indexPath)
        let index: Index = try signed(Index.self, indexPath)
        try need(index.schema == "MTGA_CN_INDEX_V1" && index.channel == profile.channel && index.sequence > 0 && index.releases.count <= 10000, "更新索引无效。")
        guard let expiry = ISO8601DateFormatter().date(from: index.expires_at) else { throw Problem(description: "更新索引有效期无效。") }
        try need(expiry > Date(), "更新索引已过期，请等待维护者刷新，或检查本机时间。")
        let seenPath = state.appendingPathComponent("seen-" + profile.channel + ".json")
        if fm.fileExists(atPath: seenPath.path) {
            let seen = try json(Seen.self, small(seenPath))
            try need(index.sequence >= seen.sequence, "更新索引版本倒退，已拒绝使用。")
            try need(index.sequence != seen.sequence || indexFP == seen.fingerprint, "同一索引序号对应不同内容，已拒绝使用。")
            try need(Date().timeIntervalSince1970 >= seen.checked_at - 300, "本机时间明显倒退，请校准时间后重试。")
        }
        try save(Seen(sequence: index.sequence, fingerprint: indexFP, checked_at: Date().timeIntervalSince1970), seenPath)
        let identities = index.releases.map { $0.client_version + "/" + $0.steam_build + "/" + String($0.revision) }
        try need(Set(identities).count == identities.count && Set(index.releases.map(\.id)).count == index.releases.count, "索引中存在重复版本。")
        let refs = index.releases.filter { $0.client_version == game.version && $0.steam_build == game.build }.sorted { $0.revision > $1.revision }
        guard let ref = refs.first else { return nil }
        try basename(ref.id); try basename(ref.manifest.name); try validFP(ref.manifest.fingerprint)
        let release = try signed(Release.self, fetch(ref.manifest.name, expected: ref.manifest.fingerprint))
        try validateRelease(release, ref, game, profile.channel)
        try activeResources(game.root, release.resources)
        if let old = try journal(), old.client_version == release.client_version && old.steam_build == release.steam_build { try need(release.revision >= old.revision, "拒绝安装低于本机已记录版本的发布包。") }
        return release
    }
    func guardGame(_ j: Journal, partial: Bool) throws {
        let now = try inspectGame(library)
        try need(now.root == game.root && now.version == j.client_version && now.build == j.steam_build, "游戏版本发生变化，拒绝用旧记录覆盖当前文件。")
        try verifyProtected(game.root, j.protected_files)
        for r in j.rows { let fp = try fingerprint(target(game.root, r.relative)); try need(fp == r.after || (partial && fp == r.before), "资源存在未知变化，已停止覆盖：\(r.layer)") }
    }
    func restore(_ old: Journal) throws {
        var j = old; try stopped(); try guardGame(j, partial: true)
        for row in j.rows { try verify(backup(j, row), row.before) }
        j.state = "rolling-back"; try save(j, journalURL)
        for row in j.rows.reversed() {
            let dest = try target(game.root, row.relative)
            if try fingerprint(dest) == row.before { continue }
            try copyVerified(backup(j, row), dest, row.before) { try stopped(); try self.guardGame(j, partial: true) }
        }
        for row in j.rows { try verify(target(game.root, row.relative), row.before) }
        try verifyProtected(game.root, j.protected_files)
        j.state = "rolled-back"; try save(j, journalURL)
        print("已恢复到这次更新前的实际文件状态。")
    }
    func update(_ release: Release, dryRun: Bool) throws {
        if !dryRun { try stopped() }
        let old = try journal()
        try need(old == nil || ["installed", "rolled-back"].contains(old!.state), "上次操作未完成，请回到菜单选择 5「撤销上次更新」。")
        try verifyProtected(game.root, release.protected_files)
        if let old = old, old.state == "installed" && old.release_id == release.id, try release.resources.allSatisfy({ try fingerprint(target(game.root, $0.relative)) == $0.output }) { try guardGame(old, partial: false); print("当前已安装此版本，无需重复更新。"); return }
        let required = release.resources.reduce(Int64(0)) { $0 + $1.input.bytes * 2 + $1.output.bytes * 2 + $1.patch.fingerprint.bytes } + 64 * 1024 * 1024
        let free = (try fm.attributesOfFileSystem(forPath: state.path)[.systemFreeSize] as? NSNumber)?.int64Value ?? 0
        let gameFree = (try fm.attributesOfFileSystem(forPath: game.root.path)[.systemFreeSize] as? NSNumber)?.int64Value ?? 0
        let gameRequired = release.resources.reduce(Int64(0)) { $0 + max($1.input.bytes, $1.output.bytes) }
        try need(gameFree >= gameRequired + 64 * 1024 * 1024, "游戏所在磁盘空间不足，无法安全替换或恢复资源。")
        try need(free >= required, "磁盘空间不足，无法同时保存下载文件和更新前备份。请先腾出空间后重试。")
        let id = UUID().uuidString; let work = state.appendingPathComponent("transactions").appendingPathComponent(id); try folder(work)
        var rows: [Row] = []
        for row in release.resources {
            let current = try target(game.root, row.relative), actual = try fingerprint(current)
            // An exact desired output is already authenticated by the selected signed
            // release. Preserve it without inventing an official reconstruction base.
            if actual == row.output {
                try copyVerified(current, work.appendingPathComponent("before-" + row.layer), actual)
                try copyVerified(current, work.appendingPathComponent("after-" + row.layer), row.output)
                var available = false
                if actual == row.input {
                    try copyVerified(current, work.appendingPathComponent("original-" + row.layer), row.input)
                    available = true
                } else if let previous = old, previous.client_version == game.version, previous.steam_build == game.build,
                          let r = previous.rows.first(where: { $0.layer == row.layer && $0.relative == row.relative && $0.original == row.input && $0.original_available != false }) {
                    let knownBase = original(previous, r)
                    try verify(knownBase, row.input)
                    try copyVerified(knownBase, work.appendingPathComponent("original-" + row.layer), row.input)
                    available = true
                }
                rows.append(Row(layer: row.layer, relative: row.relative, before: actual, after: row.output, original: row.input, original_available: available))
                continue
            }
            var source: URL? = actual == row.input ? current : nil
            if source == nil, let previous = old, previous.client_version == game.version, previous.steam_build == game.build,
               let r = previous.rows.first(where: { $0.layer == row.layer && $0.relative == row.relative && $0.original == row.input && $0.original_available != false }) {
                // A verified official base can rebuild this output even if another patch changed current bytes.
                source = original(previous, r); try verify(source!, row.input)
            }
            try copyVerified(current, work.appendingPathComponent("before-" + row.layer), actual)
            if let source = source {
                try copyVerified(source, work.appendingPathComponent("original-" + row.layer), row.input)
                let patch = try fetch(row.patch.name, expected: row.patch.fingerprint, limit: 512*1024*1024, remote: row.patch.url)
                try applyDelta(work.appendingPathComponent("original-" + row.layer), patch, work.appendingPathComponent("after-" + row.layer), row.output)
            } else {
                let cachedOutput = state.appendingPathComponent("patch-cache").appendingPathComponent(row.output.sha256 + "-" + String(row.output.bytes) + ".delta")
                let names = ["card": "卡牌文字", "client": "菜单和活动文字", "resources": "界面资源", "bootstrap_fonts": "基础字体", "font_bundle": "卡牌字体"]; print("\(names[row.layer] ?? row.layer)：正在准备完整更新文件；已保存当前文件供恢复。")
                if fm.fileExists(atPath: cachedOutput.path) {
                    _ = try cacheRegular(cachedOutput)
                    // The selected signed output authenticates cached bytes even when
                    // an earlier manifest did not advertise a full download location.
                    try copyVerified(cachedOutput, work.appendingPathComponent("after-" + row.layer), row.output)
                } else {
                    guard let full = row.full else { throw Problem(description: "本机资源不是匹配的官方文件或本工具已知版本：\(row.layer)。缺少这一层的官方重建底稿，且此发布尚无完整资源，请等待维护者发布支持覆盖的版本。") }
                    let downloaded = try fetch(full.name, expected: row.output, limit: 1024*1024*1024, remote: full.url)
                    try copyVerified(downloaded, work.appendingPathComponent("after-" + row.layer), row.output)
                }
            }
            rows.append(Row(layer: row.layer, relative: row.relative, before: actual, after: row.output, original: row.input, original_available: source != nil))
        }
        var j = Journal(schema: "MTGA_CN_SUBSCRIBER_TX_V1", id: id, release_id: release.id, revision: release.revision, game: game.root.path, client_version: game.version, steam_build: game.build, rows: rows, protected_files: release.protected_files, state: "prepared")
        try guardGame(j, partial: true)
        if dryRun { print("下载和检查完成，游戏文件未修改。\n下一步：完全退出 MTGA，回到菜单选择 4「安装或更新汉化」。游戏语言必须保持 English（英语）。"); return }
        let remaining = (try fm.attributesOfFileSystem(forPath: game.root.path)[.systemFreeSize] as? NSNumber)?.int64Value ?? 0
        let needed = rows.reduce(Int64(0)) { $0 + max($1.before.bytes, $1.after.bytes) }
        try need(remaining >= needed + 64 * 1024 * 1024, "游戏磁盘剩余空间不足，尚未修改游戏。")
        try stopped(); try save(j, journalURL)
        j.state = "installing"; try save(j, journalURL)
        do {
            for row in rows {
                let dest = try target(game.root, row.relative)
                try copyVerified(work.appendingPathComponent("after-" + row.layer), dest, row.after) { try stopped(); try self.guardGame(j, partial: true) }
                #if TESTING
                if ProcessInfo.processInfo.environment["MTGA_TEST_CRASH_AFTER"] == row.layer { _exit(79) }
                #endif
            }
            try guardGame(j, partial: false); j.state = "installed"; try save(j, journalURL)
            print("更新已应用，文件检查通过。现在可以打开 MTGA，游戏语言必须保持 English（英语）。\n更新前的文件已备份；如有问题，完全退出游戏后选择 5「撤销上次更新」。")
        } catch {
            let initial = String(describing: error)
            do { try restore(j) } catch { throw Problem(description: "安装中断：\(initial)\n恢复也已停止：\(error)\n请保留文件，退出游戏后选择 5「撤销上次更新」；不要删除备份和恢复记录。") }
            throw Problem(description: "安装失败，已恢复更新前状态：\(initial)")
        }
    }
}

func options(_ args: [String]) throws -> (String, [String: String]) {
    let command = args.first ?? "help"; var values: [String: String] = [:]; var i = 1
    let flags = Set(["--state", "--profile", "--steam-library", "--key", "--input", "--output"])
    while i < args.count { try need(flags.contains(args[i]) && i+1 < args.count && values[args[i]] == nil, "参数无效，执行 help 查看说明。") ; values[args[i]] = args[i+1]; i += 2 }
    return (command, values)
}
let userGuide = """
MTGA 中文维护 · 使用说明

游戏语言必须一直保持 English（英语）。完成 Steam 和游戏内下载后，完全退出 MTGA。
打开桌面入口，依次输入数字并按回车：
  2 检查更新 → 3 下载并检查（不改游戏）→ 4 安装或更新汉化
看到“更新已应用”后再打开游戏。操作结束后按回车返回菜单，0 退出。

1 查看当前汉化；5 撤销上次更新（安装中断也用它，必须先退出游戏）。
撤销恢复到更新前实际状态，不一定是官方原版；Steam 已升级游戏时可能无法恢复。
下载失败可重选 3；没有适配版本就等待发布。出错请保留完整提示反馈。

普通汉化修正不用重装工具。安装完成后，联网安装包的 ZIP 和解压文件夹都可删除。
保留桌面入口及工具的安装目录：~/Library/Application Support/MTGAChineseSubscriber
该目录存有备份和恢复记录，不要手动删除。离线演示包需保留解压目录。

最新工具、图文步骤和反馈：https://github.com/1996Neil/mtga-chinese-updates
收到朋友转发的安装包也可从这里找到后续更新；提示需要升级工具时再重新下载安装。
"""
let help = """
MTGA 中文维护订阅客户端 v3.3（macOS + Steam）
check      检查维护者是否发布了适合当前游戏的版本
preview    下载并检查更新文件；不修改游戏
update     下载并应用已签名的匹配版本；必须先退出游戏
status     查看本机安装状态
rollback   恢复到最近一次更新前的文件状态
recover    恢复未完成的安装；游戏更新后会拒绝跨版本覆盖
setup      从当前安装包安装到用户目录（不需要管理员权限）
guide      显示中文菜单使用说明
help       显示命令行说明

默认配置在用户 Library/Application Support/MTGAChineseSubscriber/profile.json。
可用 --steam-library 指定 Steam 库目录，--state 指定独立状态目录。
日常汉化修订直接执行 update；仅工具协议升级时需要重新安装客户端。
此工具不修改主程序、动态库、账号、牌组或官方清单。它只处理签名清单允许的五层汉化资源。
"""
func main() throws {
    let (command, opt) = try options(Array(CommandLine.arguments.dropFirst()))
    let defaultState = fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/MTGAChineseSubscriber")
    let state = URL(fileURLWithPath: opt["--state"] ?? defaultState.path).standardizedFileURL.resolvingSymlinksInPath()
    if command == "help" { print(help); return }
    if command == "guide" { print(userGuide); return }
    #if TESTING
    if command == "validate-release-url" {
        guard let value = opt["--input"], let feed = opt["--profile"], let name = opt["--output"] else { throw Problem(description: "测试参数不完整。") }
        _ = try releaseURL(value, feed: feed, name: name); return
    }
    #endif
    // Publishing-only utilities never read the game or make network calls.
    if command == "keygen" {
        guard let output = opt["--output"] else { throw Problem(description: "需要 --output 私钥路径。") }
        let path = URL(fileURLWithPath: output).standardizedFileURL
        try need(!fm.fileExists(atPath: path.path), "私钥已存在，拒绝覆盖。")
        let key = Curve25519.Signing.PrivateKey(); try atomic(key.rawRepresentation, path)
        print(key.publicKey.rawRepresentation.base64EncodedString()); return
    }
    if command == "verify-envelope" {
        guard let input = opt["--input"], let publicPath = opt["--key"] else { throw Problem(description: "需要 --input 签名封装及 --key 公钥文件。") }
        let publicText = String(decoding: try small(URL(fileURLWithPath: publicPath), limit: 256), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        let envelope = try json(Envelope.self, small(URL(fileURLWithPath: input)))
        guard let keyData = Data(base64Encoded: publicText), let payload = Data(base64Encoded: envelope.payload), let signature = Data(base64Encoded: envelope.signature) else { throw Problem(description: "签名封装无效。") }
        let key = try Curve25519.Signing.PublicKey(rawRepresentation: keyData)
        try need(key.isValidSignature(signature, for: payload), "签名校验失败。")
        print("签名通过。"); return
    }
    if command == "sign" {
        guard let keyPath = opt["--key"], let input = opt["--input"], let output = opt["--output"] else { throw Problem(description: "签名需要 --key、--input、--output。") }
        let key = try Curve25519.Signing.PrivateKey(rawRepresentation: small(URL(fileURLWithPath: keyPath), limit: 32))
        let data = try small(URL(fileURLWithPath: input), limit: 2*1024*1024)
        let envelope = Envelope(payload: data.base64EncodedString(), signature: try key.signature(for: data).base64EncodedString())
        try save(envelope, URL(fileURLWithPath: output)); return
    }
    let exe = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL.resolvingSymlinksInPath()
    if command == "setup" {
        let source = URL(fileURLWithPath: opt["--profile"] ?? exe.deletingLastPathComponent().appendingPathComponent("profile.json").path)
        var profile = try json(Profile.self, small(source)); try need(profile.schema == "MTGA_CN_PROFILE_V1" && ["stable", "testing"].contains(profile.channel), "安装包配置无效。")
        guard let pub = Data(base64Encoded: profile.public_key) else { throw Problem(description: "公钥无效。") }; _ = try Curve25519.Signing.PublicKey(rawRepresentation: pub)
        if profile.feed == "./feed" {
            let localFeed = source.deletingLastPathComponent().appendingPathComponent("feed").standardizedFileURL
            try checked(localFeed, regular: false)
            profile = Profile(schema: profile.schema, feed: localFeed.path, public_key: profile.public_key, channel: profile.channel)
        }
        try folder(state)
        let setupLockPath = state.appendingPathComponent("client.lock")
        if fm.fileExists(atPath: setupLockPath.path) { try checked(setupLockPath) }
        let setupLock = open(setupLockPath.path, O_RDWR | O_CREAT | O_NOFOLLOW, 0o600)
        try need(setupLock >= 0, "无法打开安装锁。")
        defer { flock(setupLock, LOCK_UN); close(setupLock) }
        try need(flock(setupLock, LOCK_EX | LOCK_NB) == 0, "另一项维护操作正在运行，请稍后安装。")
        try setupProfile(profile, state: state)
        try atomic(Data(contentsOf: exe), state.appendingPathComponent("mtga-cn"), mode: 0o755)
        let shell = """
        #!/bin/zsh
        ROOT="${0:A:h}"
        while true; do
         print '\nMTGA 中文维护'
         print '游戏语言必须为 English（英语）；安装或撤销汉化更新前，请完全退出游戏。'
         print '  1 查看当前汉化'
         print '  2 检查更新'
         print '  3 下载并检查（不改游戏）'
         print '  4 安装或更新汉化（先备份，再安装）'
         print '  5 撤销上次更新（也用于安装中断）'
         print '  6 使用说明'
         print '  0 退出'
         read -r 'choice?请输入数字并按回车：' || exit 0
         case "$choice" in
         1) cmd=status;; 2) cmd=check;; 3) cmd=preview;; 4) cmd=update;; 5) cmd=recover;; 6) cmd=guide;; 0) exit 0;; *) print '请输入菜单中的数字。'; continue;;
         esac
         "$ROOT/mtga-cn" "$cmd" --state "$ROOT"
         read -r '?按回车返回菜单。' || exit 0
        done

        """
        try atomic(Data(shell.utf8), state.appendingPathComponent("MTGA 中文维护.command"), mode: 0o755)
        print("安装完成。以后打开：\(state.appendingPathComponent("MTGA 中文维护.command").path)\n工具已安装到本机。以后使用桌面入口；普通联网安装包的 ZIP 和解压文件夹可以删除。"); return
    }
    try need(["check", "preview", "update", "status", "rollback", "recover"].contains(command), "未知命令，请执行 help。")
    let profile = try json(Profile.self, small(URL(fileURLWithPath: opt["--profile"] ?? state.appendingPathComponent("profile.json").path)))
    let (library, game) = try discover(opt["--steam-library"])
    let client = try Client(state: state, profile: profile, library: library, game: game)
    print("MTGA \(game.version) / Steam \(game.build)；更新工具：v3.3")
    if command == "status" { if let j = try client.journal() { let labels = ["prepared": "文件已准备，安装未完成", "installing": "上次安装中断，请选择 5 撤销上次更新", "installed": "已安装", "rolling-back": "上次恢复中断，请选择 5 继续恢复", "rolled-back": "已撤销上次更新"]; print("上次更新：\(j.release_id)；状态：\(labels[j.state] ?? j.state)"); if j.state == "installed" { try client.guardGame(j, partial: false); print("本机已安装资源校验通过。") } } else { print("尚无本工具的安装记录。") }; return }
    if command == "rollback" || command == "recover" { guard let j = try client.journal() else { print("没有需要恢复的安装记录。"); return }; if j.state == "rolled-back" { print("此前已完成恢复。"); return }; try client.restore(j); return }
    guard let release = try client.selected() else { print("维护者尚未发布适合本机版本的更新。请等待适配当前游戏的汉化发布后，再选择 2「检查更新」。"); return }
    print("可用版本：\(release.id)；发布者签名校验通过。")
    if command == "check" { return }
    try client.update(release, dryRun: command == "preview")
}
do { try main() } catch { fputs("操作已停止：\(error)\n", stderr); exit(1) }
