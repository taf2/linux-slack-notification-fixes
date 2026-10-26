#!/usr/bin/env bash
# Install (or uninstall) the Slack notification-click fix for GNOME on Wayland.
#
#   ./installer.sh             install / update
#   ./installer.sh --uninstall remove everything this installs
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
APP_DIR="$HOME/.local/share/applications"

SCRIPT=slack-notification-activate.py
UNIT=slack-notification-activate.service
DESKTOP=slack.desktop
OLD_UNIT=slack-focus-on-request.service

info() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

do_uninstall() {
    info "Stopping and disabling $UNIT"
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$UNIT_DIR/$UNIT" "$BIN_DIR/$SCRIPT"
    if [[ -f "$APP_DIR/$DESKTOP" ]] && cmp -s "$APP_DIR/$DESKTOP" "$REPO_DIR/applications/$DESKTOP"; then
        info "Removing $APP_DIR/$DESKTOP (falls back to the system launcher)"
        rm -f "$APP_DIR/$DESKTOP"
        update-desktop-database "$APP_DIR" 2>/dev/null || true
    fi
    systemctl --user daemon-reload
    info "Uninstalled. Restart Slack for launcher changes to take effect."
}

check_deps() {
    local missing=()
    command -v xprop >/dev/null || missing+=("x11-utils (xprop)")
    /usr/bin/python3 -c 'import gi; gi.require_version("Gio", "2.0")' 2>/dev/null \
        || missing+=("python3-gi")
    /usr/bin/python3 -c 'import ctypes; ctypes.CDLL("libX11.so.6")' 2>/dev/null \
        || missing+=("libx11-6")
    if ((${#missing[@]})); then
        warn "missing dependencies: ${missing[*]}"
        echo "    sudo apt install x11-utils python3-gi libx11-6" >&2
        exit 1
    fi
    [[ "${XDG_SESSION_TYPE:-}" == wayland ]] \
        || warn "session is '${XDG_SESSION_TYPE:-unknown}', this fix targets GNOME on Wayland"
    command -v slack >/dev/null || warn "slack is not on PATH"
}

do_install() {
    check_deps

    info "Installing $BIN_DIR/$SCRIPT"
    install -Dm755 "$REPO_DIR/$SCRIPT" "$BIN_DIR/$SCRIPT"

    info "Installing $UNIT_DIR/$UNIT"
    install -Dm644 "$REPO_DIR/systemd/$UNIT" "$UNIT_DIR/$UNIT"

    if [[ -f "$APP_DIR/$DESKTOP" ]] && ! cmp -s "$APP_DIR/$DESKTOP" "$REPO_DIR/applications/$DESKTOP"; then
        info "Backing up existing $APP_DIR/$DESKTOP to $DESKTOP.bak"
        cp "$APP_DIR/$DESKTOP" "$APP_DIR/$DESKTOP.bak"
    fi
    info "Installing $APP_DIR/$DESKTOP (runs Slack under XWayland)"
    install -Dm644 "$REPO_DIR/applications/$DESKTOP" "$APP_DIR/$DESKTOP"
    update-desktop-database "$APP_DIR" 2>/dev/null || true

    if systemctl --user list-unit-files "$OLD_UNIT" --no-legend 2>/dev/null | grep -q .; then
        info "Disabling superseded $OLD_UNIT"
        systemctl --user disable --now "$OLD_UNIT" 2>/dev/null || true
    fi

    systemctl --user daemon-reload
    systemctl --user enable "$UNIT"
    systemctl --user restart "$UNIT"
    info "$UNIT is $(systemctl --user is-active "$UNIT")"

    if pgrep -f '/usr/lib/slack/slack' >/dev/null && ! pgrep -f '/usr/lib/slack/slack.*ozone-platform=x11' >/dev/null; then
        warn "Slack is running without --ozone-platform=x11; quit and relaunch it from the app launcher"
    fi
    info "Done. Logs: journalctl --user -u $UNIT -f"
}

case "${1:-}" in
    --uninstall|-u) do_uninstall ;;
    ""|--install) do_install ;;
    -h|--help) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
esac
