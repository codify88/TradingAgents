#!/bin/bash
# Install (or remove) the trade-agents platform service. Run once, with sudo:
#
#     sudo scripts/install-platform.sh             install / update
#     sudo scripts/install-platform.sh --dry-run   show what it would do
#     sudo scripts/install-platform.sh --uninstall remove it
#
# What it installs:
#   - the wake daemon (root): a root-owned copy of scripts/wake-daemon.sh at
#     /usr/local/libexec/tradingagents-wake and a LaunchDaemon that runs it at
#     load and every 30 minutes. It reads ~/.tradingagents/wake-schedule and keeps
#     a pmset wake before each job. It never runs code from the repo.
#   - the wake schedule, exported as you (tradingagents schedule export).
#   - the nightly LaunchAgent (as you), now running scripts/platform-run.sh:
#     the nightly run then the harvest, kept awake by caffeinate.
#   - the Hermes gateway LaunchAgent, only if `hermes` is installed;
#   - the Desk LaunchAgent (the web UI on localhost:8810, kept alive).
#
# It never changes or removes a pmset repeat you set yourself; --uninstall
# cancels only the wakes the daemon set, and points the nightly agent back at
# scripts/nightly.sh so the nightly run carries on.

set -euo pipefail

DRY_RUN=0
UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --uninstall) UNINSTALL=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ] && [ "$DRY_RUN" -eq 0 ]; then
    echo "Run with sudo: sudo $0 $*" >&2
    exit 1
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-$(id -un)}"
USER_HOME="$(eval echo "~$USER_NAME")"
USER_UID="$(id -u "$USER_NAME")"
TA_BIN="$USER_HOME/.local/bin/tradingagents"
SCHEDULE="$USER_HOME/.tradingagents/wake-schedule"

WAKE_BIN=/usr/local/libexec/tradingagents-wake
WAKE_PLIST=/Library/LaunchDaemons/com.jeremysmith.tradingagents.wake.plist
AGENTS="$USER_HOME/Library/LaunchAgents"
NIGHTLY_PLIST="$AGENTS/com.jeremysmith.tradingagents.nightly.plist"
HERMES_PLIST="$AGENTS/com.jeremysmith.tradingagents.hermes.plist"
DESK_PLIST="$AGENTS/com.jeremysmith.tradingagents.desk.plist"

say() { echo "==> $*"; }
run() {
    if [ "$DRY_RUN" -eq 1 ]; then echo "    would run: $*"; else "$@"; fi
}
as_user() { run sudo -u "$USER_NAME" "$@"; }

fill() {  # template -> destination, with placeholders filled
    local src="$1" dst="$2" owner="$3"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "    would write $dst from $src"
        return
    fi
    sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$USER_HOME#g" \
        -e "s#__HERMES__#${HERMES_BIN:-}#g" -e "s#__SCHEDULE_PATH__#$SCHEDULE#g" \
        "$src" > "$dst.tmp"
    chown "$owner" "$dst.tmp"
    mv "$dst.tmp" "$dst"
}

reload_agent() {  # plist in the user's GUI domain
    run launchctl bootout "gui/$USER_UID" "$1" 2>/dev/null || true
    run launchctl bootstrap "gui/$USER_UID" "$1"
}

if [ "$UNINSTALL" -eq 1 ]; then
    say "removing the wake daemon"
    run launchctl bootout system "$WAKE_PLIST" 2>/dev/null || true
    run rm -f "$WAKE_PLIST" "$WAKE_BIN"
    say "cancelling wakes the daemon set (owner 'tradingagents'); your pmset repeat is left alone"
    pmset -g sched | sed -nE "s/.*wake at ([0-9]{2}\/[0-9]{2}\/[0-9]{4} [0-9:]{8}) by 'tradingagents'.*/\1/p" |
    while read -r d t; do
        run pmset schedule cancel wake "$(date -j -f '%m/%d/%Y %H:%M:%S' "$d $t" '+%m/%d/%y %H:%M:%S')" tradingagents
    done
    AWAKE_PLIST="$AGENTS/com.jeremysmith.tradingagents.awake.plist"
    if [ -f "$AWAKE_PLIST" ]; then
        say "removing the stay-awake agent"
        run launchctl bootout "gui/$USER_UID" "$AWAKE_PLIST" 2>/dev/null || true
        run rm -f "$AWAKE_PLIST"
    fi
    if [ -f "$HERMES_PLIST" ]; then
        say "removing the Hermes gateway agent"
        run launchctl bootout "gui/$USER_UID" "$HERMES_PLIST" 2>/dev/null || true
        run rm -f "$HERMES_PLIST"
    fi
    if [ -f "$DESK_PLIST" ]; then
        say "removing the Desk agent"
        run launchctl bootout "gui/$USER_UID" "$DESK_PLIST" 2>/dev/null || true
        run rm -f "$DESK_PLIST"
    fi
    if [ -f "$NIGHTLY_PLIST" ]; then
        say "pointing the nightly agent back at scripts/nightly.sh"
        if [ "$DRY_RUN" -eq 0 ]; then
            sed "s#$REPO/scripts/platform-run.sh#$REPO/scripts/nightly.sh#" "$NIGHTLY_PLIST" > "$NIGHTLY_PLIST.tmp"
            chown "$USER_NAME" "$NIGHTLY_PLIST.tmp"
            mv "$NIGHTLY_PLIST.tmp" "$NIGHTLY_PLIST"
        fi
        reload_agent "$NIGHTLY_PLIST"
    fi
    say "done"
    exit 0
fi

[ -x "$TA_BIN" ] || { echo "missing $TA_BIN (install the CLI first)" >&2; exit 1; }

say "exporting the wake schedule to $SCHEDULE"
as_user "$TA_BIN" schedule export --path "$SCHEDULE"

say "installing the wake daemon (root-owned copy at $WAKE_BIN)"
run mkdir -p /usr/local/libexec
fill "$REPO/scripts/wake-daemon.sh" "$WAKE_BIN" root:wheel
run chmod 0755 "$WAKE_BIN"
fill "$REPO/scripts/launchd/com.jeremysmith.tradingagents.wake.plist" "$WAKE_PLIST" root:wheel
run chmod 0644 "$WAKE_PLIST"
run launchctl bootout system "$WAKE_PLIST" 2>/dev/null || true
run launchctl bootstrap system "$WAKE_PLIST"

say "installing the nightly agent (platform-run.sh: nightly, then harvest)"
run mkdir -p "$AGENTS" "$USER_HOME/.tradingagents/logs/nightly"
fill "$REPO/scripts/launchd/com.jeremysmith.tradingagents.nightly.plist" "$NIGHTLY_PLIST" "$USER_NAME"
reload_agent "$NIGHTLY_PLIST"

say "installing the stay-awake agent (holds sleep off 20 min after each scheduled wake)"
AWAKE_PLIST="$AGENTS/com.jeremysmith.tradingagents.awake.plist"
fill "$REPO/scripts/launchd/com.jeremysmith.tradingagents.awake.plist" "$AWAKE_PLIST" "$USER_NAME"
reload_agent "$AWAKE_PLIST"

HERMES_BIN="$(sudo -u "$USER_NAME" bash -lc 'command -v hermes' 2>/dev/null || true)"
if [ -n "$HERMES_BIN" ]; then
    say "installing the Hermes gateway agent ($HERMES_BIN), kept alive"
    fill "$REPO/scripts/launchd/com.jeremysmith.tradingagents.hermes.plist" "$HERMES_PLIST" "$USER_NAME"
    reload_agent "$HERMES_PLIST"
else
    say "Hermes not installed yet; skipping its agent (re-run this after installing it)"
fi

say "installing the Desk agent (http://localhost:8810, loopback only), kept alive"
fill "$REPO/scripts/launchd/com.jeremysmith.tradingagents.desk.plist" "$DESK_PLIST" "$USER_NAME"
reload_agent "$DESK_PLIST"

say "done. Check with: pmset -g sched   and   tradingagents schedule show"
