#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
app="menubar/build/Clapwake Bar.app"
mkdir -p "$app/Contents/MacOS" menubar/build/ModuleCache
swiftc -parse-as-library -O -warnings-as-errors \
  -target "$(uname -m)-apple-macosx14.0" \
  -module-cache-path menubar/build/ModuleCache \
  menubar/ClapwakeBar.swift -o "$app/Contents/MacOS/ClapwakeBar"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>ClapwakeBar</string>
<key>CFBundleIdentifier</key><string>com.you.clapwake.bar</string>
<key>CFBundleName</key><string>Clapwake Bar</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSUIElement</key><true/>
<key>LSMinimumSystemVersion</key><string>14.0</string>
</dict></plist>
PLIST
codesign -s - --force "$app"
case "${1:-}" in
  "") ;;
  --install)
    mkdir -p "$HOME/Applications"
    ditto "$app" "$HOME/Applications/Clapwake Bar.app"
    ;;
  *) echo "Usage: $0 [--install]" >&2; exit 2 ;;
esac
printf 'Built %s\n' "$app"
