#!/bin/bash
# Start Xvfb, fetch ima on first run, launch it under Wine with CDP enabled, keep it running.
set -euo pipefail

# Official full installer URL, as reported by ima's own updater (app_updater_win.cc in its log).
# Override both to pin another version; md5 is checked before extraction.
IMA_URL=${IMA_URL:-https://app-dl.ima.qq.com/reshub/924cd926ee/ima-app-win/formal/20261030213544660/Production/ima.copilot_win_x64_2.6.16_5304_1018_1384.exe}
IMA_MD5=${IMA_MD5:-36cb53f2d2e4817c2c83e1497f92aec7}
SCREEN=${SCREEN:-1600x1000x24}
APP=/data/app
PROFILE='C:\ima-profile'

log() { echo "[ima-entrypoint] $*" >&2; }

# ---- 1. ima binaries (Tencent's, never baked into the image) ----
if [ "$(cat $APP/.source 2>/dev/null)" != "$IMA_URL" ]; then
    log "downloading $IMA_URL"
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/ima.exe" "$IMA_URL"
    echo "$IMA_MD5  $tmp/ima.exe" | md5sum -c --quiet
    # installer = Chrome mini_installer: resource CHROME.PACKED.7Z -> chrome.7z -> Chrome-bin/
    7z x -y -o"$tmp/rs" "$tmp/ima.exe" >/dev/null
    rm -rf "$APP" && mkdir -p "$APP"
    7z x -y -o"$APP" "$(find "$tmp/rs" -iname 'chrome.7z' | head -1)" >/dev/null
    echo "$IMA_URL" > "$APP/.source"
    rm -rf "$tmp"
fi
EXE="$APP/Chrome-bin/ima.copilot.exe"

# ---- 1b. bundled extensions an agent never needs. Each one with a service worker costs a ~40 MB
# Wine process at startup and again whenever a tab opens (they listen to tabs.* events).
# Chromium uninstalls an external extension once its <id>.json is gone; move it back to restore.
# recorder, audio/podcast players, mind map, Google Office editing, web translate, usage stats,
# image tools, code viewer, web clipper
IMA_DROP_EXTENSIONS=${IMA_DROP_EXTENSIONS-hgpacghgeonehmbpbfgfceodbeaohlfb emagenjhmpjalpoconjkojjhiljigfoh jkfgholpigobipmfnpckjghnfbbkpecn plpfbadbocioapeolmdippopkebeapof gbkeegbaiigmenfmjfclcdgdpimamgkj jgbkoppinnkajdckajmppgnefifmgnld cdldgbooaochlonaceckeeooaajplpef jhnkclbnlaofmbgaepmgiedepkbafipg eelcllbacbedmhanfdfaapgmmnckpbhh egkgeljgkehpdelhobnmbpkjjjfmacfl}
for ext in "$APP"/Chrome-bin/*/Extensions; do
    mkdir -p "$ext.off"
    for id in $IMA_DROP_EXTENSIONS; do [ -f "$ext/$id.json" ] && mv "$ext/$id.json" "$ext.off/"; done
done

mkdir -p /data/downloads

# ---- 2. display ----
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 "$SCREEN" -nolisten tcp >/tmp/xvfb.log 2>&1 &
for _ in $(seq 50); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.1; done

# ---- 3. wine prefix (mscoree/mshtml off: their install dialogs hang an invisible display) ----
if [ ! -f "$WINEPREFIX/system.reg" ]; then
    log "creating wine prefix"
    WINEDLLOVERRIDES="mscoree,mshtml=" wineboot -i
    wineserver -w
fi
# Chromium asks for Windows CJK font names; without these every Chinese glyph in pages and
# dialogs renders as a box (page text read via DOM is unaffected, screenshots are)
if [ ! -f "$WINEPREFIX/.fonts-v1" ]; then
    for f in "Microsoft YaHei" "Microsoft YaHei UI" SimSun NSimSun SimHei "MS Shell Dlg" "MS Shell Dlg 2" "Segoe UI" "PingFang SC"; do
        wine reg add 'HKCU\Software\Wine\Fonts\Replacements' /v "$f" /t REG_SZ /d "WenQuanYi Micro Hei" /f >/dev/null 2>&1
    done
    wineserver -w
    touch "$WINEPREFIX/.fonts-v1"
fi

# ---- 4. CDP: Chrome only listens on 127.0.0.1:9222, publish it as 9223 ----
socat TCP-LISTEN:9223,fork,reuseaddr TCP:127.0.0.1:9222 &

# ---- 4b. MCP server for agents (talks to CDP on 9222 locally) ----
( cd /opt/imactl && while :; do venv/bin/python server.py >>/tmp/mcp.log 2>&1; sleep 3; done ) &

# ---- 5. ima ----
FLAGS=(
    --no-sandbox --disable-gpu --disable-direct-composition
    --disable-features=RendererCodeIntegrity
    # ima's own chrome://flags switch "Tencent Remote Debug Switch"; without it 2.6+ refuses CDP
    --enable-features=TencentRemoteDebugSwitch
    --remote-debugging-port=9222 --remote-allow-origins=*
    "--user-data-dir=$PROFILE"
)
[ "${IMA_KSM:-1}" = 1 ] && export LD_PRELOAD=/opt/wine/lib/ksm_preload.so

stopping=0
ima_pid=
graceful_stop() {
    # ima rotates its login token on every start and Chromium flushes prefs lazily; a hard kill
    # leaves the old (revoked) token on disk and the next start lands on the QR login page.
    stopping=1
    log "closing ima"
    imactl close >/dev/null 2>&1 || true
    # the wine launcher lives as long as ima.copilot.exe (no procps in the image: poll the pid)
    for _ in $(seq 30); do [ -n "$ima_pid" ] && kill -0 "$ima_pid" 2>/dev/null || break; sleep 1; done
    timeout 10 wineserver -w 2>/dev/null || wineserver -k 2>/dev/null || true
    log "closed"
    exit 0
}
trap graceful_stop TERM INT

while :; do
    # no session restore: reopened PDF/chat windows add ~350 MB to the startup peak
    rm -f "$WINEPREFIX/drive_c/ima-profile/Default/Sessions/"* 2>/dev/null || true
    log "starting ima"
    wine "$EXE" "${FLAGS[@]}" >/tmp/ima.log 2>&1 &
    ima_pid=$!
    wait $ima_pid || true
    wineserver -w 2>/dev/null || true
    [ $stopping = 1 ] && break
    log "ima exited, restarting in 5s"
    sleep 5
done
