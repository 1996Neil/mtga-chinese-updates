#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h:h}"
OUT="${1:-$ROOT/dist/subscriber/bin}"
mkdir -p "$OUT"
for ARCH in arm64 x86_64; do
  xcrun swiftc -swift-version 5 -O -target "$ARCH-apple-macos13.0" "$ROOT/tools/subscriber/Client.swift" -o "$OUT/mtga-cn-$ARCH" -lz
done
lipo -create "$OUT/mtga-cn-arm64" "$OUT/mtga-cn-x86_64" -output "$OUT/mtga-cn"
codesign --force --sign - "$OUT/mtga-cn"
codesign --verify --strict "$OUT/mtga-cn"
file "$OUT/mtga-cn"
