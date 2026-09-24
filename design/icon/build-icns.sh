#!/bin/bash
# Rebuild src/troupe/assets/icon/{troupe-1024.png,troupe.iconset/,troupe.icns} from the chosen
# 1024px master SVG. Validated end-to-end with iconutil (macOS built-in, no new dependency).
#
# Usage: design/icon/build-icns.sh design/icon/option-b-orb.svg
set -euo pipefail
SRC_SVG="$1"
OUT=src/troupe/assets/icon
STAGE=$(mktemp -d)

qlmanage -t -s 1024 -o "$STAGE" "$SRC_SVG" >/dev/null
MASTER="$STAGE/$(basename "$SRC_SVG").png"

mkdir -p "$OUT/troupe.iconset"
cp "$MASTER" "$OUT/troupe-1024.png"

# Standard macOS iconset naming — identical convention Xcode's AppIcon.appiconset uses,
# so these PNGs drop straight into an Xcode asset catalog if the GUI moves to SwiftUI.
declare -a SIZES=(
  "16 icon_16x16.png" "32 icon_16x16@2x.png"
  "32 icon_32x32.png" "64 icon_32x32@2x.png"
  "128 icon_128x128.png" "256 icon_128x128@2x.png"
  "256 icon_256x256.png" "512 icon_256x256@2x.png"
  "512 icon_512x512.png" "1024 icon_512x512@2x.png"
)
for spec in "${SIZES[@]}"; do
  size=$(echo "$spec" | cut -d' ' -f1)
  name=$(echo "$spec" | cut -d' ' -f2)
  sips -z "$size" "$size" "$MASTER" --out "$OUT/troupe.iconset/$name" >/dev/null
done

iconutil -c icns "$OUT/troupe.iconset" -o "$OUT/troupe.icns"
rm -rf "$STAGE"
echo "Built $OUT/troupe-1024.png, $OUT/troupe.iconset/, $OUT/troupe.icns"
