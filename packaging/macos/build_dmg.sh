#!/bin/bash
# Build dist/EchoPath-Installer.dmg: one double-clickable "Install EchoPath"
# that opens Terminal and runs install.sh, so people see every step it takes.
set -euo pipefail
cd "$(dirname "$0")/../.."

STAGING="$(mktemp -d)/stage"
APP="$STAGING/Install EchoPath.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" dist

cp packaging/macos/install.sh "$APP/Contents/Resources/install.sh"
if [ ! -f packaging/macos/EchoPath.icns ]; then
  .venv/bin/python -c "import sys; sys.path.insert(0, 'packaging/macos'); import make_app, subprocess, shutil, pathlib; d = pathlib.Path('$STAGING/icon.iconset'); make_app.render_icon(d); subprocess.run(['iconutil', '-c', 'icns', str(d), '-o', 'packaging/macos/EchoPath.icns'], check=True); shutil.rmtree(d)"
fi
cp packaging/macos/EchoPath.icns "$APP/Contents/Resources/EchoPath.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Install EchoPath</string>
  <key>CFBundleDisplayName</key><string>Install EchoPath</string>
  <key>CFBundleIdentifier</key><string>com.zeroshotmind.echopath.installer</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleExecutable</key><string>installer</string>
  <key>CFBundleIconFile</key><string>EchoPath</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/installer" <<'EXEC'
#!/bin/bash
# Open Terminal and run the bundled install.sh in view.
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
osascript <<OSA
tell application "Terminal"
  activate
  do script "clear; echo '◉  EchoPath installer'; echo; bash '$RES/install.sh'"
end tell
OSA
EXEC
chmod +x "$APP/Contents/MacOS/installer"

# Ad-hoc signature: unsigned bundles are refused outright on newer macOS.
codesign --force --deep -s - "$APP"

cat > "$STAGING/READ ME FIRST.txt" <<'TXT'
◉ EchoPath — lets your coding agent use your Mac's desktop, and learns from it.

To install: right-click "Install EchoPath" → Open → Open.

(A plain double-click is blocked the first time because this download is
not notarized by Apple. EchoPath is built on your Mac instead, and the
Terminal window that opens shows exactly what the installer does.)

The installer puts EchoPath.app in /Applications and opens it in your menu
bar. Building it on your Mac is what lets macOS grant Accessibility and
Screen Recording to EchoPath by name.

Requirements: macOS 13+, Xcode Command Line Tools, and a Claude Code or
Codex login for the model bridge.
TXT

rm -f dist/EchoPath-Installer.dmg
hdiutil create -volname "Install EchoPath" -srcfolder "$STAGING" -ov -format UDZO dist/EchoPath-Installer.dmg >/dev/null
echo "built dist/EchoPath-Installer.dmg ($(du -h dist/EchoPath-Installer.dmg | cut -f1))"
