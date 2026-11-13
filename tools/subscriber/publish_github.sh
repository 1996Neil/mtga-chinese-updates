#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h:h:h}"
if (( $# != 4 )); then
  print '用法：publish_github.sh <已审候选目录> <修订号> <本机验收JSON> <全新输出目录>' >&2
  exit 2
fi
CLIENT="$ROOT/dist/subscriber/bin/mtga-cn"
KEYS="$HOME/Library/Application Support/MTGAChinesePublisher/signing"
REPO='1996Neil/mtga-chinese-updates'
RUN="${1:A}"
REVISION="$2"
ACCEPTANCE="${3:A}"
OUT="${4:A}"
[[ -x "$CLIENT" && -f "$KEYS/subscription-v1.ed25519" ]] || { print '请先构建客户端并配置本机发布密钥。' >&2; exit 1; }
IDENTITY="$(python3 -c 'import json,sys; x=json.load(open(sys.argv[1])); print(x["client_version"]+"-"+x["steam_build"])' "$RUN/snapshot.json")"
TAG="mtga-$IDENTITY-r$REVISION"
python3 "$ROOT/tools/subscriber/publisher.py" --run "$RUN" --revision "$REVISION" --minimum-client 4 --include-full --local-acceptance "$ACCEPTANCE" --output "$OUT/package" --client "$CLIENT" --key "$KEYS/subscription-v1.ed25519" --public-key "$(cat "$KEYS/subscription-v1.public.txt")" --feed "https://raw.githubusercontent.com/$REPO/main/feed" --asset-base "https://github.com/$REPO/releases/download/$TAG"
python3 "$ROOT/tools/subscriber/package_installer.py" --client "$CLIENT" --profile "$OUT/package/profile.json" --output "$OUT/MTGA-Chinese-Subscriber-v3.3"
python3 "$ROOT/tools/subscriber/github_publish.py" publish --package "$OUT/package" --repo "$REPO" --tag "$TAG" --client "$CLIENT" --public-key-file "$KEYS/subscription-v1.public.txt" --key "$KEYS/subscription-v1.ed25519" --installer "$OUT/MTGA-Chinese-Subscriber-v3.3.zip" --receipt "$OUT/publication.json"
