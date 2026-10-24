#!/bin/zsh
set -euo pipefail
task_dir="${0:A:h}"
task_project="${task_dir:h:h}"
task_out="$task_project/compatibility/card-language-2026-10-07"
task_app="$task_out/MTGA 中文读牌.app"
mkdir -p "$task_app/Contents/MacOS" "$task_app/Contents/Resources"
python3 "$task_dir/export_cards.py" --output "$task_app/Contents/Resources/cards.json"
swiftc -swift-version 5 -O -target arm64-apple-macosx14.0 \
  "$task_dir/CardOverlay.swift" -o "$task_app/Contents/MacOS/CardOverlay" \
  -framework AppKit -framework SwiftUI -framework ScreenCaptureKit -framework Vision -framework CryptoKit
cat > "$task_app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>local.neil.mtga.cardreader</string>
<key>CFBundleName</key><string>MTGA 中文读牌</string>
<key>CFBundleDisplayName</key><string>MTGA 中文读牌</string>
<key>CFBundleExecutable</key><string>CardOverlay</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.2.0</string>
<key>CFBundleVersion</key><string>2</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>LSUIElement</key><true/>
<key>NSHighResolutionCapable</key><true/>
<key>NSScreenCaptureUsageDescription</key><string>只在本机识别 MTGA 英文卡牌大图，并显示中文读牌浮窗。画面不保存、不上传。</string>
</dict></plist>
PLIST
codesign --force --sign - "$task_app"
"$task_app/Contents/MacOS/CardOverlay" --self-test --data "$task_app/Contents/Resources/cards.json"
print -r -- "Built: $task_app"
