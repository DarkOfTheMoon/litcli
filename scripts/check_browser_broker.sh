#!/usr/bin/env bash
# Acceptance run for the browser broker (design: docs/superpowers/specs/2026-11-27-browser-broker-design.md).
#
# One check per item of the design's acceptance list, each with its own data dir and its own
# browser, every observation written under $EVID. `--only N` runs one item; `--list` names them.
# Real Chrome and the network are needed for most items — a missing precondition is a SKIP with
# its reason recorded, never a silent PASS.
#
# The user's own profile is only read, except where an item says otherwise: browsers this script
# starts are stopped again, and `browser stop` -- which leaves the user's browser alone -- is the
# last thing each such item does.
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
CLI=$REPO/bin/litcli
PY=$(command -v python3)
EVID=${EVID:-${TMPDIR:-/tmp}/litcli-broker-acceptance}
ONLY=${1:-}
[ "$ONLY" = "--only" ] && ONLY=${2:-}
KIDS=()

export PYTHONPATH=$REPO
mkdir -p "$EVID"

note() { printf '%s\n' "$*" | tee -a "$EVID/run.log"; }
ok()   { printf 'PASS %s\n' "$*"; }
bad()  { printf 'FAIL %s\n' "$*"; FAILED=$((FAILED + 1)); }
skip() { local n=$1; shift; printf 'SKIP %s: %s\n' "$n" "$*"; SKIPPED=$((SKIPPED + 1)); }
#: $1 = command, $2 = check number, $3 = what it is for
need_cmd() { command -v "$1" >/dev/null || { skip "$2" "$1 is not installed ($3)"; return 1; }; }
#: $1 = check number
need_display() { [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || { skip "$1" "no display"; return 1; }; }

cleanup() {
    for pid in "${KIDS[@]:-}"; do
        [ -n "$pid" ] && kill "$pid" 2>/dev/null
    done
    # `stop` leaves a headed instance up by design, so a run would otherwise end with its
    # windows still on screen. Every browser this script starts names a directory under
    # $EVID; the user's own profile is only reached by the headless checks, which `stop` has
    # already killed.
    local pid
    for pid in $(pgrep -f -- "user-data-dir=$EVID/" 2>/dev/null); do kill "$pid" 2>/dev/null; done
    # A browser that is already shutting down from the check's own `stop` can swallow that
    # TERM; whatever is left a second later gets one it cannot.
    sleep 1
    for pid in $(pgrep -f -- "user-data-dir=$EVID/" 2>/dev/null); do kill -9 "$pid" 2>/dev/null; done
}
trap cleanup EXIT

# -- shared fixtures -----------------------------------------------------------

write_fixtures() {
    cat > "$EVID/client.py" <<'PY'
"""One client: start a session, download, then report what the browser holds.

With a marker path it waits there first, so the caller can look while both clients are alive.
"""
import os
import sys
import time

from litcli.browser import Session

profile, url, out = sys.argv[1], sys.argv[2], sys.argv[3]
session = Session(profile=None if profile == "-" else profile, headless=True,
                  profile_source="config")
session.start()
print("ready", flush=True)  # the caller waits for this instead of guessing at a sleep
open(out, "wb").write(session.download(url))
marker = sys.argv[4] if len(sys.argv) > 4 else None
while marker and not os.path.exists(marker):
    time.sleep(0.05)
pages = session._runtime.context.pages
print(f"pages {len(pages)}")
print(f"own {session._page_.url}")
print(f"same {sum(1 for page in pages if page.url == session._page_.url)}")
session.close()
PY
    cat > "$EVID/slow_server.py" <<'PY'
"""A server that streams one file slowly, so a client can be caught mid-download."""
import http.server
import sys
import time

CHUNKS, DELAY = 8, 0.4
BODY = b"%PDF-1.4\n" + b"x" * 2048 * 1024


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        step = len(BODY) // CHUNKS
        for start in range(0, len(BODY), step):
            try:
                self.wfile.write(BODY[start:start + step])
                self.wfile.flush()
            except BrokenPipeError:
                return
            time.sleep(DELAY)


http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
PY
    mkdir -p "$EVID/files"
    printf '%%PDF-1.4 alpha\n' > "$EVID/files/alpha.pdf"
    printf '%%PDF-1.4 bravo\n' > "$EVID/files/bravo.pdf"
}

new_env() {  # $1 = check number: a private data dir for this run
    export XDG_DATA_HOME=$EVID/n$1/data XDG_CONFIG_HOME=$EVID/n$1/config
    mkdir -p "$XDG_DATA_HOME" "$XDG_CONFIG_HOME"
    SESSION=$XDG_DATA_HOME/litcli/browser-session.json
    # A rerun in the same evidence dir inherits the last run's file: wait_session passes on
    # a broker that is gone, and the pid read out of it is dead.
    rm -f "$SESSION"
}

#: $1 = profile dir: kill anything left on it from an earlier run and start it empty
fresh_profile() {
    pkill -9 -f "user-data-dir=$1" 2>/dev/null
    sleep 0.2
    rm -rf "$1"
    mkdir -p "$1"
}

config_for() {  # $1 = profile dir: point the config at it, so login works on this broker
    mkdir -p "$XDG_CONFIG_HOME/litcli"
    printf '[browser]\nprofile = "%s"\n' "$1" >"$XDG_CONFIG_HOME/litcli/config.toml"
}

#: $1 = control port: hold a lease until killed (the "someone is using it" case)
hold_lease() {
    "$PY" - "$1" <<'PYHOLD' &
import json, socket, sys, time
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2)
s.sendall(b'{"cmd":"hello","version":1,"headless":true,"pid":1}\n')
s.makefile().readline()
time.sleep(60)
PYHOLD
    KIDS+=($!)
}

port_of() { "$PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }

wait_session() {  # $1 = seconds
    for _ in $(seq 1 $(( $1 * 10 ))); do
        [ -s "$SESSION" ] && return 0
        sleep 0.1
    done
    return 1
}

wait_profile_free() {  # $1 = seconds; wait until no browser holds or answers on the default profile
    # A Chrome that has just been killed keeps answering on its debug port for a moment; a probe
    # run against the default profile would attach to that dying browser instead of seeing what
    # the next check sets up.
    for _ in $(seq 1 $(( $1 * 5 ))); do
        "$PY" -c 'import sys; from pathlib import Path
from litcli.browser_broker import live_port, profile_holder
p = Path(sys.argv[1])
sys.exit(0 if live_port(p) is None and profile_holder(p) is None else 1)' \
            "$HOME/.config/google-chrome" 2>/dev/null && return 0
        sleep 0.2
    done
    return 1
}

end_their_browser() {  # close the browser a check opened on the default profile, and wait it out
    # `kill $!` only reaches the launcher wrapper; the browser keeps the profile, so the next
    # check would meet a live port (or a live lock) it did not set up. Kill the real process:
    # the one listening for CDP, and the one holding the singleton lock.
    local port pid
    # Only a port that still answers: the file may name one a browser no longer serves.
    port=$("$PY" -c 'import sys; from pathlib import Path
from litcli.browser_broker import live_port
print(live_port(Path(sys.argv[1])) or "")' "$HOME/.config/google-chrome" 2>/dev/null || true)
    if [ -n "$port" ]; then
        pid=$(ss -ltnpH "sport = :$port" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | head -1 || true)
        [ -n "$pid" ] && kill "$pid" 2>/dev/null
    fi
    pid=$("$PY" -c 'import sys; from pathlib import Path
from litcli.browser_broker import profile_holder
print(profile_holder(Path(sys.argv[1])) or "")' "$HOME/.config/google-chrome" 2>/dev/null || true)
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
    wait_profile_free 30 || true
}

start_broker() {  # profile, then extra broker argv
    local profile=$1; shift
    "$PY" -m litcli.browser_broker --profile "$profile" "$@" >>"$EVID/broker.log" 2>&1 &
    KIDS+=($!)
    wait_session 40
}

stop_all() {  # this check's broker (leaves whatever browser it does not own running)
    [ -s "$SESSION" ] && "$CLI" browser stop >>"$EVID/stop.log" 2>&1
    for pid in "${KIDS[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null; done
    KIDS=()
}

local_server() {  # serve $EVID/files on a free port; prints the port
    "$PY" -m http.server "$1" --bind 127.0.0.1 --directory "$EVID/files" \
        >"$EVID/http.log" 2>&1 &
    KIDS+=($!)
    sleep 0.5
}

# -- the items -----------------------------------------------------------------

check_1() {  # two processes download at once: two pages, both complete
    new_env 1
    local_server 8701
    start_broker "$EVID/n1/prof" --headless --source config || { skip 1 "no broker"; return; }
    local marker=$EVID/n1/go
    "$PY" "$EVID/client.py" "$EVID/n1/prof" http://127.0.0.1:8701/alpha.pdf "$EVID/a.out" "$marker" \
        >"$EVID/a.log" 2>&1 &
    local a=$!
    "$PY" "$EVID/client.py" "$EVID/n1/prof" http://127.0.0.1:8701/bravo.pdf "$EVID/b.out" "$marker" \
        >"$EVID/b.log" 2>&1 &
    local b=$!
    for _ in $(seq 1 600); do
        [ -s "$EVID/a.out" ] && [ -s "$EVID/b.out" ] && break
        sleep 0.1
    done
    touch "$marker"          # both clients look now, while both are still attached
    wait $a $b
    grep -q '^%PDF-1.4 alpha' "$EVID/a.out" 2>/dev/null || { bad 1 "client A did not get its bytes"; stop_all; return; }
    grep -q '^%PDF-1.4 bravo' "$EVID/b.out" 2>/dev/null || { bad 1 "client B did not get its bytes"; stop_all; return; }
    if grep -q '^own .*alpha[.]pdf' "$EVID/a.log" && grep -q '^own .*bravo[.]pdf' "$EVID/b.log" &&
        grep -q '^same 1' "$EVID/a.log" && grep -q '^same 1' "$EVID/b.log" &&
        grep -Eq '^pages [2-9]' "$EVID/a.log" && grep -Eq '^pages [2-9]' "$EVID/b.log"; then
        ok 1 "two concurrent downloads, each on a page of its own, both still holding their address"
    else
        bad 1 "pages were shared: $(cat "$EVID/a.log" "$EVID/b.log")"
    fi
    stop_all
}

check_2() {  # SIGKILL the other client mid-download: the first still gets full bytes
    new_env 2
    "$PY" "$EVID/slow_server.py" 8702 >>"$EVID/slow.log" 2>&1 &
    local srv=$!
    start_broker "$EVID/n2/prof" --headless --source config || { skip 2 "no broker"; return; }
    "$PY" "$EVID/client.py" "$EVID/n2/prof" http://127.0.0.1:8702/x.pdf "$EVID/a2.out" >"$EVID/a2.log" 2>&1 &
    local a=$!
    sleep 1.2
    "$PY" - <<'PY' >"$EVID/b2.log" 2>&1 &
import sys
from litcli.browser import Session
s = Session(profile=sys.argv[1], headless=True, profile_source="config")
s.start()
s.download("http://127.0.0.1:8702/y.pdf")
PY
    local b=$!
    sleep 1.5
    kill -9 "$b" 2>/dev/null
    wait $a 2>/dev/null
    local size
    size=$(stat -c %s "$EVID/a2.out" 2>/dev/null || echo 0)
    if [ "$size" -eq $((2048 * 1024 + 9)) ] || [ "$size" -gt 20000000 ]; then
        ok 2 "client A got $size bytes while B was SIGKILLed"
    else
        bad 2 "client A got $size bytes (expected the whole file)"
    fi
    kill "$srv" 2>/dev/null
    stop_all
}

check_3() {  # SIGKILL the broker: the next call self-heals, and leases do count
    new_env 3
    local_server 8703
    start_broker "$EVID/n3/prof" --headless --source config || { skip 3 "no broker"; return; }
    # A held lease must show in the count -- a pid/port probe alone would not prove it.
    "$PY" - "$SESSION" >"$EVID/lease.log" 2>&1 <<'PYHOLD' &
import json, socket, sys, time
port = json.load(open(sys.argv[1]))["control_port"]
s = socket.create_connection(("127.0.0.1", port), timeout=2)
s.sendall(b'{"cmd":"hello","version":1,"headless":true,"pid":1}\n')
s.makefile().readline()
time.sleep(8)
PYHOLD
    KIDS+=($!)
    sleep 1
    local control leases
    control=$(port_of "$SESSION" control_port)
    leases=$("$PY" - "$REPO" "$control" <<'PYLEASE'
import sys
sys.path.insert(0, sys.argv[1])
from litcli import browser_broker as b
print(b.status(int(sys.argv[2]))["leases"])
PYLEASE
)
    local bpid
    bpid=$(port_of "$SESSION" pid)
    kill -9 "$bpid" 2>/dev/null
    sleep 0.5
    if "$PY" "$EVID/client.py" "$EVID/n3/prof" http://127.0.0.1:8703/alpha.pdf "$EVID/a3.out" >>"$EVID/a3.log" 2>&1 &&
        grep -q '^%PDF' "$EVID/a3.out"; then
        if [ "${leases:-0}" -ge 1 ]; then
            ok 3 "self-healed after the kill; the lease count had shown $leases"
        else
            bad 3 "self-healed, but the lease count never rose ($leases)"
        fi
    else
        bad 3 "the call after the kill did not succeed: $(tail -2 "$EVID/a3.log")"
    fi
    stop_all
}

check_4() {  # idle exit, pinned hold, and idle 0
    new_env 4
    start_broker "$EVID/n4/prof" --headless --idle-minutes 0.05 --source config ||
        { skip 4 "no broker"; return; }
    local pid
    pid=$(port_of "$SESSION" pid)
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    if kill -0 "$pid" 2>/dev/null; then bad 4 "an idle broker did not exit"; else ok 4 "an idle broker exited"; fi

    new_env 4b
    start_broker "$EVID/n4b/prof" --headless --idle-minutes 0.05 --source config --pinned ||
        { skip 4 "no second broker"; return; }
    pid=$(port_of "$SESSION" pid)
    sleep 8
    if kill -0 "$pid" 2>/dev/null; then ok 4 "a pinned broker stayed"; else bad 4 "a pinned broker exited"; fi
    stop_all

    new_env 4c
    start_broker "$EVID/n4c/prof" --headless --idle-minutes 0 --source config ||
        { skip 4 "no third broker"; return; }
    pid=$(port_of "$SESSION" pid)
    sleep 8
    if kill -0 "$pid" 2>/dev/null; then ok 4 "idle-minutes 0 stayed"; else bad 4 "idle-minutes 0 exited"; fi
    stop_all
}

check_5() {  # a protocol mismatch: replaced when unused, kept when used, copies intact
    new_env 5
    "$PY" "$EVID/slow_server.py" 8705 >>"$EVID/slow.log" 2>&1 &
    local srv=$!
    # An older broker: the module says protocol 99.
    "$PY" -c 'import sys, litcli.browser_broker as b; b.PROTOCOL_VERSION = 99; sys.exit(b.main(sys.argv[1:]))' \
        --profile "$EVID/n5/prof" --headless --source config >>"$EVID/broker.log" 2>&1 &
    KIDS+=($!)
    wait_session 40 || { skip 5 "no broker"; return; }
    local old
    old=$(port_of "$SESSION" pid)
    # Hold a lease on the old broker first: the first v1 client to arrive would otherwise
    # replace it (the rule keys on leases, and it is then the only one).
    "$PY" - "$SESSION" <<'PYHOLD' &
import json, socket, sys, time
s = socket.create_connection(("127.0.0.1", json.load(open(sys.argv[1]))["control_port"]), timeout=2)
s.sendall(b'{"cmd":"hello","version":1,"headless":true,"pid":1}\n')
s.makefile().readline()
time.sleep(25)
PYHOLD
    KIDS+=($!)
    sleep 0.5
    "$PY" "$EVID/client.py" "$EVID/n5/prof" http://127.0.0.1:8705/full.pdf "$EVID/a5.out" >>"$EVID/a5.log" 2>&1 &
    local a=$!
    for _ in $(seq 1 300); do grep -q '^ready' "$EVID/a5.log" && break; sleep 0.1; done
    # Somebody now holds a lease on the old broker: the newcomer must keep it and say so once.
    "$PY" "$EVID/client.py" "$EVID/n5/prof" http://127.0.0.1:8705/sub.pdf "$EVID/b5.out" >"$EVID/b5.log" 2>&1
    wait $a 2>/dev/null
    local kept=no
    kill -0 "$old" 2>/dev/null && kept=yes
    if [ "$kept" = yes ] && grep -qi "protocol" "$EVID/b5.log"; then
        ok 5 "the in-use old broker was kept and noticed"
    else
        bad 5 "old broker kept=$kept, notice: $(grep -ci protocol "$EVID/b5.log")"
    fi
    if [ "$(stat -c %s "$EVID/a5.out" 2>/dev/null || echo 0)" -gt 1000 ]; then
        ok 5 "the in-flight copy was not truncated"
    else
        bad 5 "the in-flight copy was truncated"
    fi
    kill "$srv" 2>/dev/null
    stop_all

    new_env 5b
    "$PY" -c 'import sys, litcli.browser_broker as b; b.PROTOCOL_VERSION = 99; sys.exit(b.main(sys.argv[1:]))' \
        --profile "$EVID/n5b/prof" --headless --source config >>"$EVID/broker.log" 2>&1 &
    KIDS+=($!)
    wait_session 40 || { skip 5 "no second broker"; return; }
    old=$(port_of "$SESSION" pid)
    local_server 8705
    "$PY" "$EVID/client.py" "$EVID/n5b/prof" http://127.0.0.1:8705/alpha.pdf "$EVID/c5.out" >>"$EVID/c5.log" 2>&1
    if kill -0 "$old" 2>/dev/null; then bad 5 "an unused old broker was not replaced"; else ok 5 "an unused old broker was replaced"; fi
    stop_all
}

check_6() {  # the three probes, and no browser at all while the section is off
    if "$PY" -m unittest tests.test_browser.AliveProbeTest tests.test_browser.SessionTest \
        >"$EVID/probes.log" 2>&1; then
        ok 6 "bare kill / companion really alive / companion only pretending: probes green"
    else
        bad 6 "probe tests failed: $(tail -3 "$EVID/probes.log")"
    fi
    new_env 6
    "$PY" - "$REPO" >"$EVID/off.log" 2>&1 <<'PYOFF'
import sys
sys.path.insert(0, sys.argv[1])
from litcli.browser import session_from_config
print("session:", session_from_config({"browser": {"enabled": False}}))
PYOFF
    if grep -qx "session: None" "$EVID/off.log" && [ ! -s "$SESSION" ]; then
        ok 6 "enabled = false yields no session and writes no discovery file"
    else
        bad 6 "enabled = false: $(cat "$EVID/off.log")"
    fi
}

check_7() {  # no Chrome on PATH: the bundled browser is used
    need_chromium() { "$PY" -c 'from litcli.browser_broker import _bundled_binary; print(_bundled_binary())' >"$EVID/bundled.log" 2>&1; }
    need_chromium || { skip 7 "no bundled Chromium (run: litcli backends install-browser)"; return; }
    new_env 7
    local shim=$EVID/shim
    mkdir -p "$shim"
    for name in python3 python3.12 sh bash env cat dirname readlink mkdir sleep ps grep; do
        [ -e "/usr/bin/$name" ] && ln -sf "/usr/bin/$name" "$shim/$name"
    done
    local_server 8707
    PATH=$shim "$PY" -m litcli.browser_broker --profile "$EVID/n7/prof" --headless --source config \
        >>"$EVID/broker.log" 2>&1 &
    KIDS+=($!)
    if ! wait_session 40; then skip 7 "the broker did not come up without Chrome"; return; fi
    if PATH=$shim "$PY" "$EVID/client.py" "$EVID/n7/prof" http://127.0.0.1:8707/alpha.pdf "$EVID/a7.out" \
        >>"$EVID/a7.log" 2>&1 && grep -q '^%PDF' "$EVID/a7.out"; then
        ok 7 "pages were fetched through the bundled browser"
    else
        bad 7 "no page through the bundled browser: $(tail -2 "$EVID/a7.log")"
    fi
    stop_all
}

check_8() {  # login against a headless broker: replaces when idle, waits, or refuses
    need_display 8 || return

    # 1. Nobody holds it: the hidden browser is replaced by a visible one.
    new_env 8a
    config_for "$EVID/n8a/prof"
    start_broker "$EVID/n8a/prof" --headless --source config || { skip 8 "no broker"; return; }
    local old
    old=$(port_of "$SESSION" pid)
    "$CLI" browser login springer </dev/null >"$EVID/login8a.log" 2>&1
    local code=$?
    local now
    now=$(port_of "$SESSION" pid 2>/dev/null || echo 0)
    if [ "$code" -eq 0 ] && grep -q 'Sign in to springer' "$EVID/login8a.log" &&
        [ "$now" != "$old" ] && ! tr '\0' '\n' <"/proc/$now/cmdline" 2>/dev/null |
            grep -qx -- '--headless=new'; then
        ok 8 "an idle headless broker was replaced by a visible one"
    else
        bad 8 "idle: exit $code, pid $old -> $now: $(head -2 "$EVID/login8a.log")"
    fi
    stop_all

    # 2. Somebody holds it: refuse with the sentence, and leave the broker alone.
    new_env 8b
    config_for "$EVID/n8b/prof"
    start_broker "$EVID/n8b/prof" --headless --source config || { skip 8 "no broker"; return; }
    old=$(port_of "$SESSION" pid)
    hold_lease "$(port_of "$SESSION" control_port)"
    sleep 0.5
    "$CLI" browser login springer </dev/null >"$EVID/login8b.log" 2>&1
    code=$?
    if grep -q 'someone is using the browser' "$EVID/login8b.log" && [ "$code" -eq 2 ] &&
        kill -0 "$old" 2>/dev/null && [ -s "$SESSION" ]; then
        ok 8 "a held browser made login refuse with exit 2, and the broker stayed"
    else
        bad 8 "held: exit $code, alive $(kill -0 "$old" 2>/dev/null && echo y || echo n): "\
"$(head -2 "$EVID/login8b.log")"
    fi
    stop_all

    # 3. The lease is let go inside the wait: login carries on instead of refusing.
    new_env 8c
    config_for "$EVID/n8c/prof"
    start_broker "$EVID/n8c/prof" --headless --source config || { skip 8 "no broker"; return; }
    hold_lease "$(port_of "$SESSION" control_port)"
    local holder=${KIDS[-1]}
    sleep 0.5
    "$CLI" browser login springer </dev/null >"$EVID/login8c.log" 2>&1 &
    local login=$!
    sleep 2
    kill "$holder" 2>/dev/null
    wait $login
    code=$?
    if [ "$code" -eq 0 ] && grep -q 'Sign in to springer' "$EVID/login8c.log"; then
        ok 8 "a lease let go during the wait let the login through"
    else
        bad 8 "released: exit $code: $(head -2 "$EVID/login8c.log")"
    fi
    stop_all
}

check_9() {  # Windows: unit tests here, the real machine is 待核
    if "$PY" -m unittest tests.test_browser_broker.WindowsTest >"$EVID/windows.log" 2>&1; then
        ok 9 "the Windows shapes are unit-tested (real Windows run: 待核)"
    else
        bad 9 "Windows unit tests failed"
    fi
}

check_10() {  # their Chrome with a debug port is attached, not shadowed
    local chrome
    chrome=$(command -v google-chrome || command -v google-chrome-stable || true)
    [ -n "$chrome" ] || { skip 10 "no Chrome"; return; }
    need_display 10 || return
    new_env 10
    # The browser processes (children carry --type=) on their directory: attaching must not
    # add one, i.e. no second Chrome may be started next to theirs.
    mains10() {
        local pid
        for pid in $(pgrep -f -- "--user-data-dir=$HOME/.config/google-chrome"); do
            tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -qv -- "--type=" && echo "$pid"
        done | sort -u
    }
    ours10() {  # ... of those, ones this repo started: ours are headless, theirs is a window
        local pid
        for pid in $(mains10); do
            tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q -- "--headless" && echo "$pid"
        done
    }
    # A broker a previous run left on this profile would heal mid-check and start a browser of
    # its own, which reads as a second one; an old session file is not consulted here.
    local pid
    for pid in $(pgrep -f -- "litcli.browser_broker --profile $HOME/.config/google-chrome" || true); do
        kill "$pid" 2>/dev/null
    done
    # A headless browser a previous run left on their directory owns the profile lock and the
    # port file: a fresh launch would hand over to it and exit, and the pid watched below would
    # be a corpse. A visible window of theirs is left alone -- the launch hands over to that
    # one, which is the very case this check is about.
    for pid in $(ours10); do kill "$pid" 2>/dev/null; done
    for _ in $(seq 1 100); do
        [ -z "$(ours10)" ] && break
        sleep 0.1
    done
    rm -f ~/.config/google-chrome/DevToolsActivePort   # it would outlive the browser killed above
    # Their own browser, started by hand with a port -- the case that must not be shadowed.
    "$chrome" --user-data-dir="$HOME/.config/google-chrome" --remote-debugging-port=0 \
        --no-first-run --no-default-browser-check about:blank \
        >>"$EVID/theirs10.log" 2>&1 &
    local theirs=$!
    KIDS+=($theirs)
    for _ in $(seq 1 100); do
        [ -s ~/.config/google-chrome/DevToolsActivePort ] && break
        sleep 0.2
    done
    if [ ! -s ~/.config/google-chrome/DevToolsActivePort ]; then
        skip 10 "their Chrome opened no port"; kill "$theirs" 2>/dev/null; return
    fi
    # Either the launch stays (ours10 was cleared above) or it hands over to a window of theirs
    # already open and exits; in both cases the browser to watch is the one browser that is on
    # their directory after the launch settled.
    for _ in $(seq 1 50); do
        [ "$(mains10 | wc -l)" -eq 1 ] && break
        sleep 0.1
    done
    local mains_before
    mains_before=$(mains10)
    if [ -z "$mains_before" ] || [ "$(printf '%s\n' "$mains_before" | wc -l)" -ne 1 ]; then
        bad 10 "expected one browser on their directory, saw '$mains_before'"
        kill "$theirs" 2>/dev/null
        end_their_browser
        return
    fi
    theirs=$mains_before
    "$PY" - >"$EVID/run10.log" 2>&1 <<'PY'
import sys
sys.path.insert(0, ".")
from litcli.browser import Session
s = Session(headless=True, profile_source="default")
s.start()
print("mode", s.mode)
print("broker pid", s._lease.owns)
s.close()
PY
    : >"$EVID/stop.log"   # only this check's stop line may be read below
    # Keep their browser out of the teardown: `browser stop` (not the harness) must be what
    # leaves it running. It is the last pid added to KIDS, so dropping the last suffices.
    KIDS=("${KIDS[@]:0:${#KIDS[@]}-1}")
    "$CLI" browser stop >>"$EVID/stop.log" 2>&1
    local mains_after
    mains_after=$(mains10)
    if grep -qx 'mode attached' "$EVID/run10.log" && grep -qx 'broker pid False' "$EVID/run10.log" \
            && grep -q "your Chrome was left running" "$EVID/stop.log" \
            && [ "$mains_before" = "$theirs" ] && [ "$mains_after" = "$theirs" ] \
            && kill -0 "$theirs" 2>/dev/null; then
        ok 10 "their browser with a port was attached to, not shadowed; stop left it running"
    else
        bad 10 "$(cat "$EVID/run10.log") $(cat "$EVID/stop.log") main pids: '$mains_before' -> '$mains_after', theirs $theirs"
    fi
    kill "$theirs" 2>/dev/null
    end_their_browser
}

check_11() {  # their Chrome without a port: exit 1 and the exact hint
    local chrome
    chrome=$(command -v google-chrome || command -v google-chrome-stable || true)
    [ -n "$chrome" ] || { skip 11 "no Chrome"; return; }
    wait_profile_free 30 || true
    # The dir is named even though it is Chrome's own default: `new_env` exports XDG_CONFIG_HOME,
    # which a plain launch would follow into another check's fixture, while the session looks the
    # profile up as `~/.config/google-chrome` regardless. Naming the default dir keeps the no-port
    # rule (Chrome compares the resolved path), so this is still their portless browser.
    "$chrome" --user-data-dir="$HOME/.config/google-chrome" --no-first-run --no-default-browser-check \
        about:blank >>"$EVID/theirs.log" 2>&1 &
    local pid=$!
    KIDS+=($pid)
    sleep 6
    new_env 11
    "$PY" - <<'PY' >"$EVID/run11.log" 2>&1
import sys
sys.path.insert(0, ".")
from litcli.browser import Session
s = Session(profile=None, headless=True)
try:
    s.start()
    print("started")
except Exception as e:
    print(type(e).__name__, e)
PY
    if grep -q "without a debugging port" "$EVID/run11.log"; then
        ok 11 "a portless running Chrome is reported, not crashed into"
    else
        bad 11 "got: $(tail -2 "$EVID/run11.log")"
    fi
    kill "$pid" 2>/dev/null
    end_their_browser
}

check_12() {  # only a stale DevToolsActivePort: still "no port", no dead connect
    new_env 12
    mkdir -p "$EVID/n12/prof"
    printf '5599\n/x\n' > "$EVID/n12/prof/DevToolsActivePort"
    "$PY" - <<PY >"$EVID/run12.log" 2>&1
import sys
sys.path.insert(0, "$REPO")
from litcli.browser import Session
s = Session(profile="$EVID/n12/prof", headless=True, profile_source="config")
try:
    s.start()
    print("cdp", s._lease.cdpport)
except Exception as e:
    print(type(e).__name__, e)
PY
    if grep -q '^cdp ' "$EVID/run12.log" &&
        ! grep -q "5599" "$EVID/run12.log"; then
        ok 12 "a stale port file was not connected to"
    else
        bad 12 "got: $(tail -2 "$EVID/run12.log")"
    fi
    stop_all
}

check_13() {  # a client's own page: the one already open is not navigated
    new_env 13
    local_server 8713
    start_broker "$EVID/n13/prof" --headless --source config || { skip 13 "no broker"; return; }
    # Their tab: opened first, and its address recorded.
    "$PY" - "$EVID/n13/prof" <<'PY' >"$EVID/theirs13.log" 2>&1
import sys
sys.path.insert(0, ".")
from litcli.browser import Session
s = Session(profile=sys.argv[1], headless=True, profile_source="config")
s.start()
s.page("http://127.0.0.1:8713/alpha.pdf")
print("their-url", s._page_.url)
s.close()
PY
    "$PY" "$EVID/client.py" "$EVID/n13/prof" http://127.0.0.1:8713/bravo.pdf "$EVID/a13.out" >>"$EVID/a13.log" 2>&1
    if grep -q 'alpha.pdf' "$EVID/theirs13.log" && grep -q 'bravo.pdf' "$EVID/a13.log" &&
        ! grep -q 'alpha.pdf' "$EVID/a13.log"; then
        ok 13 "the first page kept its address; the client used another"
    else
        bad 13 "pages: $(cat "$EVID/theirs13.log" "$EVID/a13.log")"
    fi
    stop_all
}

check_14() {  # their profile with their Chrome closed: a headless browser on it works
    new_env 14
    wait_profile_free 30 || true
    local_server 8714
    "$PY" - <<'PY' >"$EVID/run14.log" 2>&1
import sys
sys.path.insert(0, ".")
from litcli import paths
from litcli.browser import Session
s = Session(profile=None, headless=True)
s.start()
info = __import__("json").loads(paths.browser_session_file().read_text())
print("profile", info["profile"], "owns", info["owns"])
jar = {c.get("name") for c in s._runtime.context.cookies("https://www.sciencedirect.com")}
print("cookies", len(jar), "session", "SD_REMOTEACCESS" in jar)
s.page("http://127.0.0.1:8714/alpha.pdf")
print("ok")
PY
    if grep -q "ok$" "$EVID/run14.log"; then
        local cookies
        cookies=$(grep -o 'cookies [0-9]*' "$EVID/run14.log" | cut -d' ' -f2)
        if grep -q "$HOME/.config/google-chrome" "$EVID/run14.log" \
                && grep -q "session True" "$EVID/run14.log"; then
            ok 14 "launched on the user's own profile ($cookies SD cookies, session cookie set)"
        elif ! grep -q "$HOME/.config/google-chrome" "$EVID/run14.log"; then
            bad 14 "launched on $(grep profile "$EVID/run14.log")"
        else
            bad 14 "no SD_REMOTEACCESS in the jar: $(grep cookies "$EVID/run14.log")"
        fi
    elif grep -q "ProfileBusy" "$EVID/run14.log"; then
        skip 14 "your Chrome holds the profile"
    else
        bad 14 "$(tail -2 "$EVID/run14.log")"
    fi
    stop_all
    # The next item wants this profile too, and a browser that has just been killed keeps
    # holding the lock for a moment — long enough for the next start to read it as "yours".
    wait_profile_free 30 || true
}

check_15() {  # a signed-in article opens warm and lands on the article
    new_env 15
    wait_profile_free 30 || true
    local pii=${SD_PII:-S0017931017302428}
    "$PY" - <<PY >"$EVID/run15.log" 2>&1
import sys
sys.path.insert(0, "$REPO")
from litcli.browser import Session
from litcli.backends._cf_gate import is_challenge_page
s = Session(profile=None, headless=True)
s.start()
answer = s.page("https://www.sciencedirect.com/science/article/pii/$pii")
print("status", answer.status)
print("challenge", is_challenge_page(answer.text, answer.status))
print("title", (answer.text or "")[:200].replace("\n", " "))
s.close()
PY
    if grep -q '^status 200' "$EVID/run15.log" && ! grep -q "^challenge True" "$EVID/run15.log"; then
        ok 15 "200 on the article, not a challenge"
    elif grep -q '^status 404' "$EVID/run15.log"; then
        skip 15 "article" "PII $pii is 404 (set SD_PII)"
    elif grep -q "^challenge True" "$EVID/run15.log"; then
        # The site's own bot gate, not this project's: it cannot be sat out here while
        # Cloudflare's Turnstile has no route to its challenge host (see run15.log).
        skip 15 "article" "the site answered a challenge, not the article"
    elif grep -q "ProfileBusy" "$EVID/run15.log"; then
        skip 15 "article" "your Chrome holds the profile"
    else
        bad 15 "$(head -3 "$EVID/run15.log")"
    fi
    stop_all
    wait_profile_free 30 || true   # the next item wants this profile too
}

check_16() {  # the profile default and the dedicated escape hatch
    new_env 16
    wait_profile_free 30 || true
    local answer
    answer=$("$PY" - <<'PY'
import sys
sys.path.insert(0, ".")
from litcli import paths
print(paths.resolve_browser_profile(None) == paths.user_browser_profile_dir())
print(paths.resolve_browser_profile("dedicated") == paths.browser_profile_dir())
print(paths.resolve_browser_profile("user") == paths.user_browser_profile_dir())
PY
)
    [ "$answer" = $'True\nTrue\nTrue' ] && ok 16 "empty/user resolve to the user profile, dedicated to ours" ||
        bad 16 "resolution: $(echo "$answer" | tr '\n' ' ')"
    # The header is unbuffered out first, then one row per answered site as it lands. The whole
    # table costs minutes (an unanswered site waits its gate out), so the run is stopped as soon
    # as the first row -- sciencedirect's -- is in: a row that only showed up with the whole
    # table would mean the printing went back to buffering.
    PYTHONUNBUFFERED=1 "$CLI" browser status >"$EVID/status16.log" 2>&1 &
    local status_pid=$!
    for _ in $(seq 1 480); do   # a challenged site has taken four minutes here; leave headroom
        grep -qE '^sciencedirect ' "$EVID/status16.log" && break
        sleep 1
    done
    kill "$status_pid" 2>/dev/null
    wait "$status_pid" 2>/dev/null
    if grep -q "source: default" "$EVID/status16.log"; then
        ok 16 "status reports source default"
    else
        bad 16 "status header: $(head -3 "$EVID/status16.log")"
    fi
    # sciencedirect is the site whose own reload used to come back as an `error:` line: its row
    # must carry a state word instead.
    if grep -qE '^sciencedirect +(signed in|signed out|gated|no answer)$' "$EVID/status16.log"; then
        ok 16 "the row was printed as it was probed, with a state"
    else
        bad 16 "no sciencedirect row: $(grep -E '^sciencedirect ' "$EVID/status16.log" || echo 'row never printed')"
    fi
    stop_all
}

check_17() {  # the migration notice, and its absence once the key is set
    new_env 17
    local dedicated=$EVID/n17/dedicated
    mkdir -p "$dedicated/Default"
    printf 'cookie\n' > "$dedicated/Default/Cookies"
    "$PY" - "$REPO" "$dedicated" >"$EVID/notice17.log" 2>&1 <<'PYNOTE'
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from litcli import paths
paths.browser_profile_dir = lambda: Path(sys.argv[2])
from litcli.commands import browser as cmd
cmd._session({"browser": {}})                      # key unset: the notice is due
cmd._session({"browser": {"profile": sys.argv[2]}})  # key set: it is not
PYNOTE
    if [ "$(grep -c 'profile = "dedicated"' "$EVID/notice17.log")" -eq 1 ]; then
        ok 17 "the notice fires once, only while the key is unset, and names the escape hatch"
    else
        bad 17 "notice count $(grep -c 'profile = "dedicated"' "$EVID/notice17.log"): $(tail -2 "$EVID/notice17.log")"
    fi
}

check_18() {  # the three outcomes of stop, each checked against the process
    new_env 18
    # nobody runs: nothing to stop, and that is not an error
    "$CLI" browser stop >"$EVID/stop18a.log" 2>&1
    grep -q "no session" "$EVID/stop18a.log" && ok 18 "no session is exit 0" ||
        bad 18 "no-session wording: $(cat "$EVID/stop18a.log")"

    # ours and headless: the browser itself must go, and the broker with it
    fresh_profile "$EVID/n18/prof"
    start_broker "$EVID/n18/prof" --headless --source config || { skip 18 "no broker"; return; }
    local broker browser
    broker=$(port_of "$SESSION" pid)
    "$CLI" browser stop >"$EVID/stop18b.log" 2>&1
    browser=$(sed -n 's/.*stopped (pid \([0-9]*\)).*/\1/p' "$EVID/stop18b.log")
    for _ in $(seq 1 120); do
        kill -0 "$broker" 2>/dev/null || break
        sleep 0.1
    done
    if [ -n "$browser" ] && ! kill -0 "$browser" 2>/dev/null && ! kill -0 "$broker" 2>/dev/null; then
        ok 18 "a headless instance and its broker are both gone after stop"
    else
        bad 18 "headless: browser '$browser', broker alive $(kill -0 "$broker" 2>/dev/null && echo yes || echo no), stop said: $(cat "$EVID/stop18b.log")"
    fi

    # ours and headed: the window stays, its pid is printed, the broker goes
    need_display 18 || return
    new_env 18c
    fresh_profile "$EVID/n18c/prof"
    start_broker "$EVID/n18c/prof" --source config || { skip 18 "no headed broker"; return; }
    broker=$(port_of "$SESSION" pid)
    "$CLI" browser stop >"$EVID/stop18c.log" 2>&1
    browser=$(sed -n 's/.*left running (pid \([0-9]*\)).*/\1/p' "$EVID/stop18c.log")
    for _ in $(seq 1 120); do
        kill -0 "$broker" 2>/dev/null || break
        sleep 0.1
    done
    if [ -n "$browser" ] && kill -0 "$browser" 2>/dev/null && ! kill -0 "$broker" 2>/dev/null &&
        grep -q "close the window to end it" "$EVID/stop18c.log"; then
        ok 18 "a headed instance survives (pid $browser), said so, and the broker went"
    else
        bad 18 "headed: browser '$browser', stop said: $(cat "$EVID/stop18c.log")"
    fi
    [ -n "$browser" ] && kill "$browser" 2>/dev/null

    # attached (their Chrome, our broker): their pid stays, the broker goes, and stop says so
    local chrome theirs
    chrome=$(command -v google-chrome || command -v google-chrome-stable || true)
    if [ -z "$chrome" ]; then
        skip 18 "no Chrome for the attached row"
    else
        new_env 18d
        rm -f ~/.config/google-chrome/DevToolsActivePort
        "$chrome" --user-data-dir="$HOME/.config/google-chrome" --remote-debugging-port=0 \
            --no-first-run --no-default-browser-check about:blank \
            >>"$EVID/theirs18.log" 2>&1 &
        theirs=$!
        KIDS+=($theirs)
        for _ in $(seq 1 120); do
            [ -s ~/.config/google-chrome/DevToolsActivePort ] && break
            sleep 0.2
        done
        if [ ! -s ~/.config/google-chrome/DevToolsActivePort ]; then
            bad 18 "their Chrome opened no port"
        else
            "$PY" - >"$EVID/run18d.log" 2>&1 <<'PY'
import sys
sys.path.insert(0, ".")
from litcli.browser import Session
s = Session(headless=True, profile_source="default")
s.start()
print("mode", s.mode)
print("owns", s._lease.owns)
s.close()
PY
            broker=$(port_of "$SESSION" pid)
            "$CLI" browser stop >"$EVID/stop18d.log" 2>&1
            for _ in $(seq 1 120); do
                kill -0 "$broker" 2>/dev/null || break
                sleep 0.1
            done
            if grep -qx 'mode attached' "$EVID/run18d.log" &&
                grep -qx 'owns False' "$EVID/run18d.log" && ! kill -0 "$broker" 2>/dev/null &&
                kill -0 "$theirs" 2>/dev/null &&
                grep -q "your Chrome was left running" "$EVID/stop18d.log"; then
                ok 18 "an attached browser survives stop, the broker goes, and stop says which"
            else
                bad 18 "attached: $(sed -n 's/^mode /mode /p;s/^owns /owns /p' "$EVID/run18d.log" | tr '\n' ' ')" \
                    "broker alive $(kill -0 "$broker" 2>/dev/null && echo yes || echo no)" \
                    "theirs alive $(kill -0 "$theirs" 2>/dev/null && echo yes || echo no)" \
                    "stop said: $(cat "$EVID/stop18d.log")"
            fi
        fi
        stop_all
        kill "$theirs" 2>/dev/null
    fi
    true
}


check_19() {  # a frozen-style launch boots a broker, on the named default profile, cookies included
    new_env 19
    rm -f "$SESSION"   # a stale one would let `wait_session` pass on a broker that is gone
    # The shape a frozen build spawns: no `python -m` there, and `cli.main` has to hand the
    # flag to the module. A broker that does not boot leaves a build that can never start one.
    fresh_profile "$EVID/n19/prof"
    "$CLI" -m litcli.browser_broker --profile "$EVID/n19/prof" --headless --source config \
        >>"$EVID/broker19a.log" 2>&1 &
    KIDS+=($!)
    local browser=""
    if wait_session 40; then
        browser=$(port_of "$SESSION" browser_pid)
    fi
    # The browser can go a moment after the session is written (a replacement is what a dead
    # one gets); the argv is read until it is there or the broker has plainly failed.
    for _ in $(seq 1 50); do
        [ -e "/proc/$browser/cmdline" ] && break
        sleep 0.1
    done
    if [ -e "/proc/$browser/cmdline" ]; then
        ok 19 "the frozen argv shape (litcli -m litcli.browser_broker) boots a broker"
        # Chrome rewrites its own argv with spaces between the words, so both separators split.
        if tr '\0 ' '\n\n' <"/proc/$browser/cmdline" | grep -qx -- "--user-data-dir=$EVID/n19/prof"; then
            ok 19 "the browser is started with the directory it was given"
        else
            bad 19 "browser argv: $(tr '\0' ' ' <"/proc/$browser/cmdline" 2>/dev/null)"
        fi
    else
        bad 19 "the frozen argv shape did not boot: $(tail -3 "$EVID/broker19a.log")"
    fi
    stop_all
    # Left to itself, a headless Chrome picks a throwaway scoped_dir with an empty cookie store,
    # so nothing the user signed in would be read. Naming the default directory is not enough
    # either: Chrome refuses a port while the name resolves to its platform default, and gets
    # that default from XDG_CONFIG_HOME. Both environments are run: the plain one the harness
    # does not export, and the harness one -- in the plain one the child must be handed a
    # private XDG, the very thing whose absence made the harness run look green.
    new_env 19b
    rm -f "$SESSION"
    for mode in unset harness; do
        # Per mode: the directory is asserted, and one left by the other mode would pass it
        # even if this mode never created one.
        rm -rf "$XDG_DATA_HOME/litcli/browser-xdg"
        if wait_profile_free 30; then
            :
        else
            skip 19 "your Chrome holds the default profile ($mode)"
            continue
        fi
        probe_default_profile "$mode" "$EVID/run19-$mode.log"
        stop_all
        judge_default_profile "$mode" "$EVID/run19-$mode.log"
    done
}

#: $1 = unset|harness, $2 = log: start a session on the default profile, record what it got
probe_default_profile() {
    local runner=()
    [ "$1" = harness ] || runner=(env -u XDG_CONFIG_HOME)
    "${runner[@]}" "$PY" - <<'PY' >"$2" 2>&1
import glob
import json
import os
import sys

sys.path.insert(0, ".")
from litcli import paths
from litcli.browser import Session

patterns = [os.path.expanduser("~/.config/google-chrome-headless/scoped_dir*"),
            str(paths.browser_private_xdg() / "google-chrome-headless/scoped_dir*")]


def scopes():
    return sorted({p for pattern in patterns for p in glob.glob(pattern)})


before = scopes()
s = Session(profile=None, headless=True)
s.start()
info = json.loads(paths.browser_session_file().read_text())
with open(f"/proc/{info['browser_pid']}/environ", "rb") as fh:
    child = dict(part.split("=", 1) for part in fh.read().decode().split("\0") if "=" in part)
print("profile", info["profile"])
print("cmdline", " ".join(open(f"/proc/{info['browser_pid']}/cmdline").read().split("\0")))
print("child-xdg", child.get("XDG_CONFIG_HOME"))
jar = {c.get("name") for c in s._runtime.context.cookies("https://www.sciencedirect.com")}
print("cookies", len(jar), "session", "SD_REMOTEACCESS" in jar)
print("new-scopes", " ".join(sorted(set(scopes()) - set(before))))
s.close()
PY
}

#: $1 = unset|harness, $2 = log: the verdict on one probe_default_profile run
judge_default_profile() {
    # An old evidence log (same $EVID, older format) has no count after "cookies"; the `-gt`
    # below must not be handed an empty string.
    local cookies
    cookies=$(sed -n 's/^cookies \([0-9]*\) .*/\1/p' "$2")
    if grep -q "ProfileBusy" "$2"; then
        skip 19 "your Chrome holds the default profile ($1)"
    elif ! grep -q "^cookies" "$2"; then
        bad 19 "the default-profile launch failed ($1): $(tail -3 "$2")"
    elif ! grep -qx "profile $HOME/.config/google-chrome" "$2"; then
        bad 19 "not on the default profile ($1): $(grep '^profile' "$2")"
    elif ! grep -qF -- "--user-data-dir=$HOME/.config/google-chrome" "$2"; then
        bad 19 "the default directory was not named ($1): $(grep '^cmdline' "$2")"
    elif [ ! -d "$XDG_DATA_HOME/litcli/browser-xdg/google-chrome" ]; then
        # Chrome clobbers /proc/<pid>/environ as it starts, so the environment cannot be read
        # back: the directory it creates under the given config home proves the child had it.
        bad 19 "the child got no private XDG_CONFIG_HOME ($1): $(grep '^child-xdg' "$2")"
    elif grep -q "^new-scopes ." "$2"; then
        bad 19 "a throwaway scoped_dir was used ($1): $(grep '^new-scopes' "$2")"
    elif [ "${cookies:-0}" -gt 0 ] && grep -q "^cookies [0-9]* session True$" "$2"; then
        ok 19 "default profile named with a private child XDG, no scoped_dir, cookies read ($1)"
    else
        bad 19 "no signed-in cookies on the default profile ($1)"
    fi
}

check_20() {  # a change of profile retires the old broker instead of keeping both
    # Two clients through the ordinary session layer, one profile each — no site probes, so
    # the item stays a minute. The retirement is `ensure_lease`'s, which the CLI reaches too.
    new_env 20
    local tag old now
    "$PY" - "$EVID/n20/a" <<'PY' >"$EVID/start20a.log" 2>&1
import json
import sys

sys.path.insert(0, ".")
from litcli import paths
from litcli.browser import Session

s = Session(profile=sys.argv[1], headless=True, profile_source="config")
s.start()
print("pid", json.loads(paths.browser_session_file().read_text())["pid"])
s.close()
PY
    old=$(sed -n 's/^pid //p' "$EVID/start20a.log")
    "$PY" - "$EVID/n20/b" <<'PY' >"$EVID/start20b.log" 2>&1
import json
import sys

sys.path.insert(0, ".")
from litcli import paths
from litcli.browser import Session

s = Session(profile=sys.argv[1], headless=True, profile_source="config")
s.start()
info = json.loads(paths.browser_session_file().read_text())
print("pid", info["pid"])
print("profile", info["profile"])
s.close()
PY
    now=$(sed -n 's/^pid //p' "$EVID/start20b.log")
    for _ in $(seq 1 100); do kill -0 "$old" 2>/dev/null || break; sleep 0.1; done
    if [ -n "$old" ] && [ -n "$now" ] && [ "$now" != "$old" ] && ! kill -0 "$old" 2>/dev/null &&
        grep -qx "profile $EVID/n20/b" "$EVID/start20b.log"; then
        ok 20 "the broker of the old profile ($old) retired; profile b is served"
    else
        bad 20 "old '$old' new '$now', second start: $(tail -2 "$EVID/start20b.log")"
    fi
    stop_all
}

check_list() {  # the items, from their own header comments
    sed -n 's/^check_\([0-9]*\)() {  # /  \1  /p' "$0"
}

FAILED=0
SKIPPED=0
write_fixtures

if [ "$ONLY" = "--list" ]; then check_list; exit 0; fi
if [ -n "$ONLY" ]; then
    "check_$ONLY" || bad "$ONLY" "the check itself errored"
else
    for n in $(seq 1 20); do
        note "--- $n"
        "check_$n" || bad "$n" "the check itself errored"
    done
fi

note "evidence: $EVID"
printf '\n%d failed, %d skipped\n' "$FAILED" "$SKIPPED"
[ "$FAILED" -eq 0 ]
