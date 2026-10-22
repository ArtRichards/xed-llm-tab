#!/bin/bash
# Runs the plugin inside a real, headless xed (GTK Broadway backend) with its
# own D-Bus session, settings, config and cache, against a mock OpenAI server,
# once per display mode. Your real xed settings, plugins and API key are not
# touched.
#
# Usage: tests/integration/run.sh   (exit status 0 when every check passes)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_DIR="$(cd "$HERE/../.." && pwd)"
PORT=8765

run_mode() {
    local mode="$1" T
    T="$(mktemp -d)"
    mkdir -p "$T/data/xed/plugins" "$T/config/glib-2.0/settings" "$T/config/xed" "$T/cache"
    ln -s "$PLUGIN_DIR" "$T/data/xed/plugins/llm-tab"
    cp "$HERE/llmtabtest.py" "$HERE/llmtabtest.plugin" "$T/data/xed/plugins/"
    printf "[org/x/editor/plugins]\nactive-plugins=['llmtab', 'llmtabtest']\n" > "$T/config/glib-2.0/settings/keyfile"
    printf 'sk-test-dummy\n' > "$T/key"
    printf '{"display_mode": "%s", "base_url": "http://127.0.0.1:%s/v1", "api_key_file": "%s/key", "save_dir": "%s/saved", "style_guide_file": "%s/style.md", "query_on_open": true}\n' \
        "$mode" "$PORT" "$T" "$T" "$T" > "$T/config/xed/llm-tab.json"
    printf 'What is 2+2?\n' > "$T/notes.txt"
    printf 'Loaded file\n===\n' > "$T/other.txt"
    printf 'Answer tersely.\n' > "$T/style.md"

    python3 -I "$HERE/mock_server.py" "$PORT" "$T/requests.jsonl" & local mock=$!
    broadwayd :7 >/dev/null 2>&1 & local broadway=$!
    sleep 1

    (cd "$T" && env -u DISPLAY GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 NO_AT_BRIDGE=1 \
        XDG_DATA_HOME="$T/data" XDG_CONFIG_HOME="$T/config" XDG_CACHE_HOME="$T/cache" \
        GSETTINGS_BACKEND=keyfile GIO_USE_VFS=local GVFS_DISABLE_FUSE=1 \
        LLMTAB_TEST_DIR="$T" LLMTAB_MODE="$mode" OPENAI_API_KEY= \
        timeout 120 dbus-run-session -- xed "$T/notes.txt" >"$T/xed.log" 2>&1)
    kill $mock $broadway 2>/dev/null
    wait $mock $broadway 2>/dev/null

    echo "== $mode mode"
    python3 -I - "$T/results.json" "$T/xed.log" <<'EOF'
import json, sys
try:
    checks = json.load(open(sys.argv[1]))
except OSError:
    print("No results: xed did not finish the scenario. Log:\n" + open(sys.argv[2]).read()[-3000:])
    sys.exit(1)
for c in checks:
    print("PASS" if c["ok"] else "FAIL", c["name"], "" if c["ok"] else repr(c["detail"])[:400])
passed = sum(c["ok"] for c in checks)
print("%d / %d passed" % (passed, len(checks)))
sys.exit(0 if passed == len(checks) else 1)
EOF
    local status=$?
    # xed warns that it cannot store cursor-position metadata for the output
    # tab's file, which is never written; that one is expected.
    grep -E 'WARNING|CRITICAL|Traceback|Error' "$T/xed.log" \
        | grep -v -E 'theme|icon|a11y|AT-SPI|Set document metadata failed' | sed 's/^/  xed.log: /' | head -10
    rm -rf "$T"
    return $status
}

status=0
for mode in incognito streaming; do
    run_mode "$mode" || status=1
done
exit $status
