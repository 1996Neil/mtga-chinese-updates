import AppKit
import SwiftUI
import ScreenCaptureKit
import Vision
import ImageIO
import CryptoKit

struct Keyword: Codable, Hashable {
    let name: String
    let nameZh: String
    let explanationEn: String
    let explanationZh: String
}
struct Card: Codable {
    let name: String
    let nameZh: String
    let arenaId: Int
    let typeEn: String
    let typeZh: String
    let rules: String
    let rulesEn: String
    let translationComplete: Bool
    let flavorEn: String
    let mana: String
    let stats: String
    let keywords: [Keyword]
}
struct Library: Codable {
    struct Source: Codable { let path: String; let sha256: String }
    let schema: Int
    let versions: [String: String]
    let cards: [Card]
    let sources: [Source]
}
func normalized(_ value: String) -> String {
    value.folding(options: [.diacriticInsensitive, .caseInsensitive], locale: Locale(identifier: "en_US"))
        .unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) }.map(String.init).joined()
}
struct Line {
    let text: String
    let box: CGRect
    let confidence: Float
}
struct Match {
    let card: Card
    let titleBox: CGRect
}
final class Recognizer {
    let library: Library
    private var index: [String: Card] = [:]
    private var sourceState: [String: String] = [:]
    init(url: URL) throws {
        let data = try Data(contentsOf: url)
        guard data.count < 80_000_000 else { throw OverlayError("卡牌资料过大，请重新生成。") }
        library = try JSONDecoder().decode(Library.self, from: data)
        guard library.schema == 1 else { throw OverlayError("卡牌资料版本不支持。") }
        let grouped = Dictionary(grouping: library.cards, by: { normalized($0.name) })
        for (key, cards) in grouped where !key.isEmpty && cards.count == 1 { index[key] = cards[0] }
    }
    func checkCurrentSource() throws {
        for (i, source) in library.sources.enumerated() where i == 0 || i == 2 {
            try requireCurrentPath(source.path)
            let data = try Data(contentsOf: URL(fileURLWithPath: source.path), options: .mappedIfSafe)
            let sha = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
            guard sha == source.sha256 else {
                throw OverlayError("MTGA 卡库已更新。请运行“更新浮窗卡牌.command”后重新打开浮窗。")
            }
            sourceState[source.path] = try fingerprint(source.path)
        }
    }
    private func requireCurrentPath(_ path: String) throws {
        let url = URL(fileURLWithPath: path)
        let prefix = url.lastPathComponent.hasPrefix("Raw_CardDatabase_") ? "Raw_CardDatabase_" : "Raw_ClientLocalization_"
        let files = try FileManager.default.contentsOfDirectory(at: url.deletingLastPathComponent(),
            includingPropertiesForKeys: [.contentModificationDateKey])
            .filter { $0.lastPathComponent.hasPrefix(prefix) && $0.pathExtension == "mtga" }
        var dated: [(url: URL, date: Date)] = []
        for file in files {
            let values = try file.resourceValues(forKeys: [.contentModificationDateKey])
            dated.append((url: file, date: values.contentModificationDate ?? Date.distantPast))
        }
        let sorted = dated.sorted { a, b in
            if a.date == b.date { return a.url.lastPathComponent > b.url.lastPathComponent }
            return a.date > b.date
        }
        guard sorted.first?.url.standardizedFileURL == url.standardizedFileURL else {
            throw OverlayError("MTGA 语言资源已更新。请更新浮窗卡牌后重新打开应用。")
        }
    }
    private func fingerprint(_ path: String) throws -> String {
        let values = try URL(fileURLWithPath: path).resourceValues(forKeys: [.contentModificationDateKey, .fileSizeKey])
        return "\(values.fileSize ?? -1):\(values.contentModificationDate?.timeIntervalSince1970 ?? 0)"
    }
    func checkResourceIdentity() throws {
        for (path, state) in sourceState {
            try requireCurrentPath(path)
            guard try fingerprint(path) == state else {
                throw OverlayError("MTGA 语言资源已更改。请更新浮窗卡牌后重新打开应用。")
            }
        }
    }
    func lines(in image: CGImage, level: VNRequestTextRecognitionLevel = .accurate,
               minimumHeight: Float = 0.009) throws -> [Line] {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = level
        request.recognitionLanguages = ["en-US"]
        request.usesLanguageCorrection = false
        request.minimumTextHeight = minimumHeight
        try VNImageRequestHandler(cgImage: image).perform([request])
        return (request.results ?? []).compactMap { observation in
            guard let candidate = observation.topCandidates(1).first else { return nil }
            return Line(text: candidate.string, box: observation.boundingBox, confidence: candidate.confidence)
        }
    }
    func match(lines: [Line], imageSize: CGSize) -> Match? {
        // Require a large, exact known title PLUS nearby rule evidence. A deck-list row alone is insufficient.
        let candidates = lines.filter { $0.confidence >= 0.60 && $0.box.height >= 0.023 && $0.box.minY > 0.15 }
            .sorted { $0.box.height > $1.box.height }
        for title in candidates {
            guard let card = index[normalized(title.text)] else { continue }
            let cardWidth = min(0.72, max(title.box.width + 0.10,
                title.box.height * imageSize.height * 16 / imageSize.width))
            let nearby = lines.filter {
                $0.box.maxY < title.box.minY - 0.08 &&
                $0.box.minX >= title.box.minX - 0.06 &&
                $0.box.minX < title.box.minX + cardWidth &&
                $0.box.minY > title.box.minY - cardWidth * imageSize.width / imageSize.height * 1.55
            }.map { $0.text.lowercased() }.joined(separator: " ")
            let nameWords = Set(card.name.lowercased().split(whereSeparator: { !$0.isLetter }).map(String.init))
            let ruleWords = Set(card.rulesEn.lowercased().split(whereSeparator: { !$0.isLetter })
                .map(String.init).filter { $0.count >= 4 && !nameWords.contains($0) && $0 != "cardname" })
            let required = min(3, ruleWords.count)
            let observed = Set(nearby.split(whereSeparator: { !$0.isLetter }).map(String.init))
            if required == 0 {
                let typeWords = Set(card.typeEn.lowercased().split(whereSeparator: { !$0.isLetter }).map(String.init))
                if typeWords.contains("land") && typeWords.isSubset(of: observed) {
                    return Match(card: card, titleBox: title.box)
                }
                continue
            }
            if ruleWords.intersection(observed).count >= required {
                return Match(card: card, titleBox: title.box)
            }
        }
        return nil
    }
    func recognize(_ image: CGImage) throws -> Match? {
        match(lines: try lines(in: image), imageSize: CGSize(width: image.width, height: image.height))
    }
    func readingRegion(for title: CGRect, size: CGSize) -> CGRect {
        let width = min(0.72, max(title.width + 0.10, title.height * size.height * 16 / size.width))
        let left = max(0, title.minX - 0.045)
        let top = min(1, title.maxY + 0.035)
        let height = min(top, width * size.width / size.height * 1.5)
        return CGRect(x: left, y: max(0, top - height), width: min(width, 1 - left), height: height)
    }
    func recognizeQuick(_ image: CGImage, region: CGRect?, allowFullFallback: Bool = true) throws -> Match? {
        let size = CGSize(width: image.width, height: image.height)
        if let region {
            let pixels = CGRect(x: region.minX * size.width, y: (1 - region.maxY) * size.height,
                                width: region.width * size.width, height: region.height * size.height).integral
                .intersection(CGRect(origin: .zero, size: size))
            if let crop = image.cropping(to: pixels) {
                let actual = CGRect(x: pixels.minX / size.width, y: 1 - pixels.maxY / size.height,
                                    width: pixels.width / size.width, height: pixels.height / size.height)
                let precise = try lines(in: crop).map { line in
                    Line(text: line.text, box: CGRect(x: actual.minX + line.box.minX * actual.width,
                         y: actual.minY + line.box.minY * actual.height,
                         width: line.box.width * actual.width, height: line.box.height * actual.height), confidence: line.confidence)
                }
                if let result = match(lines: precise, imageSize: size) { return result }
            }
        }
        return allowFullFallback ? try recognize(image) : nil
    }
    func find(_ name: String) -> Card? { index[normalized(name)] }
}
struct OverlayError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
func chineseText(_ text: String) -> Bool {
    text.unicodeScalars.contains { (0x3400...0x9fff).contains($0.value) }
}

func translatedType(_ text: String) -> String {
    guard chineseText(text), text.range(of: "[A-Za-z]", options: .regularExpression) == nil else {
        return "类型译文尚未完整核对"
    }
    return text
}
func readableChinese(_ text: String, name: String = "") -> Bool {
    if text.isEmpty { return true }
    // Mana symbols and the card's translated proper name are permitted; raw templates are not.
    guard text.range(of: "[{}<>]|CLASSLEVEL", options: .regularExpression) == nil else { return false }
    let withoutName = name.isEmpty ? text : text.replacingOccurrences(of: name, with: "")
    let withoutSymbols = withoutName.replacingOccurrences(of: "\\[[WUBRGCXYZSTQEP0-9/+ ()-]+\\]", with: "", options: .regularExpression)
    return chineseText(text) && withoutSymbols.range(of: "[A-Za-z]{2,}", options: .regularExpression) == nil
}

@MainActor final class Model: ObservableObject {
    @Published var card: Card?
    @Published var status = "将鼠标停在卡牌上，展开游戏原生大图。"
    @Published var paused = false
    @Published var pinned = false
    @Published var needsPermission = false
    @Published var resourceInvalid = false
    @Published var fontSize: Double = 21 {
        didSet { UserDefaults.standard.set(fontSize, forKey: "readerBodyFontSize") }
    }
    @Published var showSettings = false
    init() {
        let saved = UserDefaults.standard.double(forKey: "readerBodyFontSize")
        fontSize = saved.isFinite && saved >= 14 && saved <= 36 ? saved : 21
    }
    var grant: () -> Void = {}
    var placement: () -> Void = {}
}

struct ReadingPanel: View {
    @ObservedObject var model: Model
    private let ink = Color(red: 0.93, green: 0.93, blue: 0.89)
    private let muted = Color(red: 0.72, green: 0.76, blue: 0.76)
    private let accent = Color(red: 0.65, green: 0.84, blue: 0.78)
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Text("中文读牌").font(.system(size: 14, weight: .semibold)).foregroundStyle(accent)
                Spacer()
                Button("字号") { model.showSettings.toggle() }
                Button(model.pinned ? "取消固定" : "固定") { model.pinned.toggle() }
                    .accessibilityHint("固定后暂不跟随其他卡牌")
                Button(model.resourceInvalid ? "需要更新" : model.paused ? "继续" : "暂停") { model.paused.toggle() }
                    .disabled(model.resourceInvalid)
                Button("退出") { NSApplication.shared.terminate(nil) }
            }
            .font(.system(size: 12)).buttonStyle(.borderless).padding(16)
            if model.showSettings {
                HStack(spacing: 12) {
                    Text("字号 \(Int(model.fontSize))").font(.system(size: 13)).monospacedDigit()
                    Button("减小") { model.fontSize = max(14, model.fontSize - 1) }.disabled(model.fontSize <= 14)
                    Slider(value: $model.fontSize, in: 14...36, step: 1).accessibilityLabel("正文大小")
                    Button("放大") { model.fontSize = min(36, model.fontSize + 1) }.disabled(model.fontSize >= 36)
                }.font(.system(size: 12)).padding(.horizontal, 16).padding(.bottom, 14)
            }
            Divider().overlay(muted.opacity(0.3))
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if let card = model.card {
                        VStack(alignment: .leading, spacing: 7) {
                            HStack(alignment: .top) {
                                Text(chineseText(card.nameZh) ? card.nameZh : "中文牌名暂缺")
                                    .font(.system(size: model.fontSize + 7, weight: .semibold))
                                    .fixedSize(horizontal: false, vertical: true)
                                Spacer(minLength: 8)
                                Text(card.mana).font(.system(size: 13, weight: .medium, design: .monospaced))
                                    .foregroundStyle(accent).padding(.top, 7)
                            }
                            Text(translatedType(card.typeZh))
                                .font(.system(size: max(14, model.fontSize - 4), weight: .medium)).foregroundStyle(accent)
                        }
                        Text(card.translationComplete && readableChinese(card.rules, name: card.nameZh) ? card.rules : "这张牌的中文规则尚未完整核对，请查看游戏原文。")
                            .font(.system(size: model.fontSize)).lineSpacing(model.fontSize * 0.3)
                            .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                        if !card.stats.isEmpty {
                            HStack { Spacer(); Text(card.stats).font(.system(size: model.fontSize + 1, weight: .semibold, design: .monospaced)) }
                        }
                        if !card.keywords.isEmpty {
                            Divider().overlay(muted.opacity(0.3))
                            ForEach(card.keywords, id: \.self) { keyword in
                                VStack(alignment: .leading, spacing: 9) {
                                    Text(readableChinese(keyword.nameZh) ? keyword.nameZh : "关键词译文暂缺")
                                        .font(.system(size: max(15, model.fontSize - 2), weight: .semibold)).foregroundStyle(accent)
                                    Text(readableChinese(keyword.explanationZh) ? keyword.explanationZh : "这项异能的中文解释尚未完整核对，请查看游戏原文。").font(.system(size: model.fontSize)).lineSpacing(model.fontSize * 0.25)
                                }.fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    } else {
                        Text(model.needsPermission ? "允许读取 MTGA 画面" : "等待英文大图")
                            .font(.system(size: 24, weight: .semibold))
                        Text(model.status).font(.system(size: 17)).lineSpacing(6).foregroundStyle(muted)
                        if model.needsPermission {
                            Button("开启屏幕录制权限") { model.grant() }.buttonStyle(.borderedProminent).tint(accent)
                            Text("只读取游戏窗口，在本机识别文字。画面不上传、不保存。")
                                .font(.system(size: 14)).lineSpacing(4).foregroundStyle(muted)
                        }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(22)
            }
            Divider().overlay(muted.opacity(0.3))
            HStack {
                Text(model.resourceInvalid ? "资料已过期，请更新后重开" : model.paused ? "已暂停识别" : model.pinned ? "已固定当前卡牌" : model.card == nil ? "等待悬停" : "中文牌面规则")
                    .font(.system(size: 11)).foregroundStyle(muted)
                Spacer()
                Button("换一侧") { model.placement() }.font(.system(size: 12)).buttonStyle(.borderless)
            }.padding(14)
        }.foregroundStyle(ink)
            .background(Color(red: 0.07, green: 0.095, blue: 0.10))
            .preferredColorScheme(.dark)
    }
}

final class FloatingPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = Model()
    let recognizer: Recognizer
    var panel: FloatingPanel!
    var menuItem: NSStatusItem!
    var timer: Timer?
    var busy = false
    var running = true
    var lastFound = Date.distantPast
    var sideRight = true
    var lastResourceCheck = Date.distantPast
    var cachedWindow: SCWindow?
    var cachedFilter: SCContentFilter?
    var windowRefresh = Date.distantPast
    var lastFullFallback = Date.distantPast
    var readingRegion: CGRect?
    let preview: String?
    init(recognizer: Recognizer, preview: String?) {
        self.recognizer = recognizer
        self.preview = preview
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.appearance = NSAppearance(named: .darkAqua)
        let frame = NSScreen.main?.visibleFrame ?? CGRect(x: 0, y: 0, width: 1440, height: 900)
        panel = FloatingPanel(contentRect: CGRect(x: frame.maxX - 438, y: frame.midY - 340,
                                                 width: 420, height: min(760, frame.height - 60)),
                              styleMask: [.titled, .closable, .resizable, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.title = "MTGA 中文读牌"
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.isMovableByWindowBackground = true
        panel.hidesOnDeactivate = false
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.minSize = NSSize(width: 360, height: 360)
        panel.contentView = NSHostingView(rootView: ReadingPanel(model: model))
        panel.isReleasedWhenClosed = false
        panel.orderFrontRegardless()
        model.grant = { [weak self] in self?.grant() }
        model.placement = { [weak self] in self?.switchSide() }
        menuItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        menuItem.button?.title = "读牌"
        let menu = NSMenu()
        menu.addItem(withTitle: "显示读牌浮窗", action: #selector(showPanel), keyEquivalent: "")
        menu.addItem(withTitle: "暂停 / 继续", action: #selector(togglePause), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出", action: #selector(quit), keyEquivalent: "q")
        for item in menu.items { item.target = self }
        menuItem.menu = menu
        if let preview {
            model.card = recognizer.find(preview)
            model.pinned = true
            model.status = "预览模式"
            return
        }
        do { try recognizer.checkCurrentSource() }
        catch { model.status = error.localizedDescription; model.paused = true; model.resourceInvalid = true; return }
        model.needsPermission = !CGPreflightScreenCaptureAccess()
        model.status = model.needsPermission ? "首次使用需在系统设置中允许“MTGA 中文读牌”读取屏幕，之后重新打开应用。" : model.status
        timer = Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.tick() }
        }
    }
    @objc func showPanel() { panel.orderFrontRegardless() }
    @objc func togglePause() { if !model.resourceInvalid { model.paused.toggle() } }
    @objc func quit() { NSApplication.shared.terminate(nil) }
    func grant() {
        _ = CGRequestScreenCaptureAccess()
        model.needsPermission = !CGPreflightScreenCaptureAccess()
        if model.needsPermission {
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!)
        }
        model.status = "允许“MTGA 中文读牌”后，退出并重新打开应用。"
    }
    func switchSide() {
        sideRight.toggle()
        let screen = panel.screen ?? NSScreen.main!
        let area = screen.visibleFrame
        panel.setFrameOrigin(NSPoint(x: sideRight ? area.maxX - panel.frame.width - 18 : area.minX + 18,
                                     y: min(max(panel.frame.minY, area.minY + 18), area.maxY - panel.frame.height)))
    }
    func applicationWillTerminate(_ notification: Notification) { running = false; timer?.invalidate() }
    func tick() async {
        guard running, !busy, !model.resourceInvalid, panel.isVisible else { return }
        if Date().timeIntervalSince(lastResourceCheck) > 5 {
            lastResourceCheck = Date()
            do { try recognizer.checkResourceIdentity() }
            catch { model.card = nil; model.status = error.localizedDescription; model.paused = true; model.resourceInvalid = true; return }
        }
        guard !model.paused, !model.pinned else { return }
        // Keep the last card readable while the user is adjusting the reader itself.
        guard !panel.frame.contains(NSEvent.mouseLocation) else { return }
        if model.needsPermission {
            model.needsPermission = !CGPreflightScreenCaptureAccess()
            if model.needsPermission { return }
        }
        guard NSWorkspace.shared.frontmostApplication?.bundleIdentifier == "com.wizards.mtga" else {
            // Never capture another application. Clear stale automatic card text.
            if Date().timeIntervalSince(lastFound) > 0.7 {
                model.card = nil
                model.status = "切回 MTGA，将鼠标停在卡牌上展开英文大图。"
            }
            return
        }
        busy = true
        defer { busy = false }
        do {
            if cachedWindow == nil || Date().timeIntervalSince(windowRefresh) > 3 {
             let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
             cachedWindow = content.windows.filter({
                $0.owningApplication?.bundleIdentifier == "com.wizards.mtga" &&
                $0.frame.width > 500 && $0.frame.height > 350
             }).max(by: { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height })
             cachedFilter = cachedWindow.map { SCContentFilter(desktopIndependentWindow: $0) }
             windowRefresh = Date()
            }
            guard let window = cachedWindow, let filter = cachedFilter else {
                model.card = nil; model.status = "未找到 MTGA 游戏窗口。"; return
            }
            let config = SCStreamConfiguration()
            let ratio = min(1.0, 1600 / window.frame.width)
            config.width = Int(window.frame.width * ratio)
            config.height = Int(window.frame.height * ratio)
            config.showsCursor = false
            let pointer = NSEvent.mouseLocation
            let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
            let reader = recognizer
            let region = readingRegion
            let allowFallback = Date().timeIntervalSince(lastFullFallback) > 0.7
            if allowFallback { lastFullFallback = Date() }
            let match = try await Task.detached(priority: .userInitiated) {
                try reader.recognizeQuick(image, region: region, allowFullFallback: allowFallback)
            }.value
            guard running, !model.paused, !model.pinned,
                  hypot(NSEvent.mouseLocation.x - pointer.x, NSEvent.mouseLocation.y - pointer.y) < 16,
                  NSWorkspace.shared.frontmostApplication?.bundleIdentifier == "com.wizards.mtga" else { return }
            if let match {
                lastFound = Date()
                readingRegion = recognizer.readingRegion(for: match.titleBox, size: CGSize(width: image.width, height: image.height))
                if model.card?.name != match.card.name {
                    model.card = match.card
                    avoidPreview(match.titleBox, window: window)
                }
            } else if Date().timeIntervalSince(lastFound) > 0.7 {
                model.card = nil
                model.status = "等待可识别的英文大图；牌表名称条目不会单独触发翻译。"
            }
        } catch {
            cachedWindow = nil; cachedFilter = nil; readingRegion = nil
            model.card = nil
            model.status = "暂时无法读取 MTGA 画面。检查屏幕录制权限，或退出后重新打开浮窗。"
        }
    }
    func avoidPreview(_ title: CGRect, window: SCWindow) {
        let quartzTop = NSScreen.screens.first?.frame.maxY ?? 0
        let center = NSPoint(x: window.frame.midX, y: quartzTop - window.frame.midY)
        guard let screen = NSScreen.screens.first(where: { $0.frame.contains(center) }) ?? NSScreen.main else { return }
        let area = screen.visibleFrame
        // Keep the pointer outside the panel so the game's hover preview stays open.
        sideRight = NSEvent.mouseLocation.x < area.midX
        let x = sideRight ? area.maxX - panel.frame.width - 18 : area.minX + 18
        panel.setFrameOrigin(NSPoint(x: x, y: max(area.minY + 18, area.maxY - panel.frame.height - 18)))
    }
}

let args = CommandLine.arguments
let resources = Bundle.main.resourceURL ?? URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
func argument(_ flag: String) -> String? {
    guard let i = args.firstIndex(of: flag), i + 1 < args.count else { return nil }
    return args[i + 1]
}
do {
    let localData = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/MTGA Card Reader/cards.json")
    let defaultData = FileManager.default.fileExists(atPath: localData.path) ? localData : resources.appendingPathComponent("cards.json")
    let dataURL = argument("--data").map { URL(fileURLWithPath: $0) } ?? defaultData
    let recognizer = try Recognizer(url: dataURL)
    if let file = argument("--recognize-image") {
      DispatchQueue.global(qos: .userInitiated).async {
       do {
        guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: file) as CFURL, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            throw OverlayError("无法读取测试图片。")
        }
        let repeats = min(10, max(1, Int(argument("--repeat") ?? "1") ?? 1))
        var match: Match?
        var elapsed: [Double] = []
        var region: CGRect?
        for _ in 0..<repeats {
            let started = ProcessInfo.processInfo.systemUptime
            match = args.contains("--legacy-ocr") ? try recognizer.recognize(image) : try recognizer.recognizeQuick(image, region: region)
            elapsed.append((ProcessInfo.processInfo.systemUptime - started) * 1000)
            if let match { region = recognizer.readingRegion(for: match.titleBox, size: CGSize(width: image.width, height: image.height)) }
        }
        let result: [String: Any] = ["matched": match?.card.name ?? "", "nameZh": match?.card.nameZh ?? "",
                                     "engine": args.contains("--legacy-ocr") ? "legacy-accurate" : "accurate-with-region-reuse",
                                     "recognitionMs": elapsed]
        let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
        print(String(decoding: data, as: UTF8.self))
        exit(match == nil ? 2 : 0)
       } catch {
        fputs("Image recognition failed: \(error.localizedDescription)\n", stderr)
        exit(1)
       }
      }
      CFRunLoopRun()
      exit(1)
    }
    if args.contains("--self-test") {
        guard let kor = recognizer.find("Kor Halberd"), kor.nameZh == "寇族长戟",
              kor.keywords.contains(where: { $0.name == "Vigilance" }),
              kor.flavorEn == "An ancient weapon for a modern war." else { throw OverlayError("翻译资料校验失败。") }
        guard translatedType("传奇神器生物 — Food Noble") == "类型译文尚未完整核对",
              !readableChinese("CLASSLEVEL [2+] [] [抓一张牌]"),
              !readableChinese("防御牌手失去{numeral}点生命。"),
              readableChinese("[W]：目标生物得+X/+X。"),
              readableChinese("SeeD学院进场时，抓一张牌。", name: "SeeD学院") else {
            throw OverlayError("中文显示边界校验失败。")
        }
        let rowOnly = [Line(text: "Kor Halberd", box: CGRect(x: 0.6, y: 0.6, width: 0.3, height: 0.04), confidence: 1)]
        guard recognizer.match(lines: rowOnly, imageSize: CGSize(width: 1600, height: 1000)) == nil else {
            throw OverlayError("仅牌表名称不应触发翻译。")
        }
        let small = [Line(text: "Kor Halberd", box: CGRect(x: 0.1, y: 0.8, width: 0.1, height: 0.012), confidence: 1),
                     Line(text: "Equipped creature gets +1/+1 and has vigilance.", box: CGRect(x: 0.1, y: 0.4, width: 0.2, height: 0.01), confidence: 1)]
        guard recognizer.match(lines: small, imageSize: CGSize(width: 1600, height: 1000)) == nil else {
            throw OverlayError("小图不应单独触发翻译。")
        }
        try recognizer.checkCurrentSource()
        if let island = recognizer.find("Island") {
            let land = [Line(text: "Island", box: CGRect(x: 0.1, y: 0.8, width: 0.2, height: 0.04), confidence: 1),
                        Line(text: island.typeEn, box: CGRect(x: 0.1, y: 0.4, width: 0.3, height: 0.025), confidence: 1)]
            guard recognizer.match(lines: land, imageSize: CGSize(width: 1600, height: 1000))?.card.name == "Island" else {
                throw OverlayError("基本地需要标题与类型行共同识别。")
            }
        }
        let testDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: testDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: testDir) }
        let oldFile = testDir.appendingPathComponent("Raw_CardDatabase_first.mtga")
        let fakeData = Data("source-update-test".utf8)
        try fakeData.write(to: oldFile)
        try FileManager.default.setAttributes([.modificationDate: Date.distantPast], ofItemAtPath: oldFile.path)
        let fakeHash = SHA256.hash(data: fakeData).map { String(format: "%02x", $0) }.joined()
        let testLibrary = Library(schema: 1, versions: [:], cards: [kor],
                                  sources: [Library.Source(path: oldFile.path, sha256: fakeHash)])
        let testURL = testDir.appendingPathComponent("cards.json")
        try JSONEncoder().encode(testLibrary).write(to: testURL)
        let testReader = try Recognizer(url: testURL)
        try testReader.checkCurrentSource()
        try testReader.checkResourceIdentity()
        try fakeData.write(to: testDir.appendingPathComponent("Raw_CardDatabase_new.mtga"))
        var rejectedUpdate = false
        do { try testReader.checkResourceIdentity() } catch { rejectedUpdate = true }
        guard rejectedUpdate else { throw OverlayError("新增卡库必须停止使用旧资料。") }
        print("PASS: database identity/hash, Chinese rules, Chinese display/template guards, source flavor/keywords, deck-row/small-card rejection, basic-land evidence, retained-old-file update rejection")
        exit(0)
    }
    MainActor.assumeIsolated {
        let app = NSApplication.shared
        let delegate = AppDelegate(recognizer: recognizer, preview: argument("--preview"))
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
} catch {
    fputs("MTGA 中文读牌: \(error.localizedDescription)\n", stderr)
    if !args.contains("--self-test") && argument("--recognize-image") == nil {
        let alert = NSAlert()
        alert.messageText = "中文读牌无法启动"
        alert.informativeText = error.localizedDescription
        alert.addButton(withTitle: "好")
        alert.runModal()
    }
    exit(1)
}
