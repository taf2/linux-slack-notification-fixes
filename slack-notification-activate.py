#!/usr/bin/python3
"""Let GNOME raise Slack when one of its notifications is clicked.

On click, GNOME Shell emits an ActivationToken signal (a startup-notification
id like "gnome-shell/Slack/7648-376-host_TIME1371471040") before ActionInvoked.
Slack/Electron ignores that signal and writes _NET_STARTUP_ID with the wrong
type (STRING instead of UTF8_STRING), so Mutter never completes the startup
sequence: the cursor spins until timeout and focus-stealing prevention keeps
the Slack window from being raised.

This forwards the token onto Slack's XWayland window as a proper UTF8_STRING
_NET_STARTUP_ID. Mutter reacts to that property change on a mapped window by
activating it with the token's timestamp. The startup sequence itself is an
X11 startup-notification one, so it is then ended with a "remove:" message,
which stops the spinner.

Slack also ignores clicks on some notifications that are still in GNOME's
tray (e.g. older ones in a conversation). To cover that, Slack's
NEW_NOTIFICATION log entries (team/channel/message/thread) are paired by time
with the ids GNOME returns for Slack's Notify calls (seen via a read-only
busctl monitor). If Slack doesn't log CLICK_NOTIFICATION shortly after a
click, the conversation is opened through a slack:// deep link instead.
"""

import collections
import ctypes
import json
import subprocess
import threading
import time
import urllib.parse

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

TOKEN_MARKER = "/Slack/"
WAIT_FOR_WINDOW_SECS = 3.0
PAIR_WINDOW_SECS = 3.0  # max gap between Slack's log entry and its Notify call
CLICK_GRACE_MS = 1500  # how long Slack gets to handle a click itself
MAX_TRACKED = 500


def xprop(*args):
    return subprocess.run(
        ["xprop", *args], capture_output=True, text=True, check=False
    ).stdout


class _ClientMessageData(ctypes.Union):
    _fields_ = [("b", ctypes.c_char * 20), ("l", ctypes.c_long * 5)]


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", _ClientMessageData),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("xclient", _XClientMessageEvent), ("pad", ctypes.c_long * 24)]


def end_startup_sequence(token):
    """Broadcast a startup-notification "remove:" for token (stops the spinner)."""
    x = ctypes.CDLL("libX11.so.6")
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XDefaultRootWindow.restype = ctypes.c_ulong
    x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x.XInternAtom.restype = ctypes.c_ulong
    x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x.XCreateSimpleWindow.restype = ctypes.c_ulong
    x.XCreateSimpleWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + [ctypes.c_int] * 2 \
        + [ctypes.c_uint] * 3 + [ctypes.c_ulong] * 2
    x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                             ctypes.c_long, ctypes.POINTER(_XEvent)]
    for fn in ("XFlush", "XCloseDisplay"):
        getattr(x, fn).argtypes = [ctypes.c_void_p]
    x.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]

    dpy = x.XOpenDisplay(None)
    if not dpy:
        print("cannot open X display", flush=True)
        return
    try:
        root = x.XDefaultRootWindow(dpy)
        sender = x.XCreateSimpleWindow(dpy, root, -100, -100, 1, 1, 0, 0, 0)
        atom_begin = x.XInternAtom(dpy, b"_NET_STARTUP_INFO_BEGIN", 0)
        atom_more = x.XInternAtom(dpy, b"_NET_STARTUP_INFO", 0)
        escaped = token.replace("\\", "\\\\").replace('"', '\\"')
        msg = f'remove: ID="{escaped}"'.encode() + b"\0"
        for i in range(0, len(msg), 20):
            ev = _XEvent()
            ev.xclient.type = 33  # ClientMessage
            ev.xclient.send_event = 1
            ev.xclient.window = sender
            ev.xclient.message_type = atom_begin if i == 0 else atom_more
            ev.xclient.format = 8
            chunk = msg[i:i + 20]
            ctypes.memmove(ctypes.addressof(ev.xclient.data), chunk, len(chunk))
            x.XSendEvent(dpy, root, 0, 1 << 22, ctypes.byref(ev))  # PropertyChangeMask
        x.XFlush(dpy)
        x.XDestroyWindow(dpy, sender)
        x.XFlush(dpy)
    finally:
        x.XCloseDisplay(dpy)


def slack_windows():
    clients = xprop("-root", "_NET_CLIENT_LIST")
    for wid in clients.split("#", 1)[-1].replace(",", " ").split():
        if wid.startswith("0x") and "slack" in xprop("-id", wid, "WM_CLASS").lower():
            yield wid


def forward_token(token, deadline):
    windows = list(slack_windows())
    if not windows:
        # Slack may be closed to the tray and still mapping its window.
        if time.monotonic() < deadline:
            return True  # retry
        print(f"no slack window for {token}", flush=True)
        return False
    for wid in windows:
        subprocess.run(
            ["xprop", "-id", wid, "-f", "_NET_STARTUP_ID", "8u",
             "-set", "_NET_STARTUP_ID", token],
            check=False,
        )
    print(f"forwarded {token} to {' '.join(windows)}", flush=True)
    # Let Mutter activate the window via the sequence before ending it.
    GLib.timeout_add(400, lambda: end_startup_sequence(token))
    return False


class NotificationTracker:
    """Map GNOME notification ids to the Slack conversation they belong to.

    All methods run on the GLib main loop.
    """

    def __init__(self):
        self.entries = collections.deque()  # (time, info) from Slack's log
        self.ids = collections.deque()  # (time, gnome id) from Notify replies
        self.by_id = collections.OrderedDict()
        self.last_click_logged = 0.0

    def add_entry(self, info):
        self.entries.append((time.monotonic(), info))
        self._pair()

    def add_id(self, nid):
        self.ids.append((time.monotonic(), nid))
        self._pair()

    def _pair(self):
        while self.entries and self.ids:
            (te, info), (ti, nid) = self.entries[0], self.ids[0]
            if abs(te - ti) > PAIR_WINDOW_SECS:
                # Unpaired leftovers (e.g. a non-message notification).
                (self.entries if te < ti else self.ids).popleft()
                continue
            self.entries.popleft()
            self.ids.popleft()
            self.by_id[nid] = info
            while len(self.by_id) > MAX_TRACKED:
                self.by_id.popitem(last=False)
            print(f"tracked notification {nid} -> {info.get('channel')}", flush=True)

    def check_click(self, nid, clicked_at):
        # Slack can log the click just before we see GNOME's token.
        if self.last_click_logged >= clicked_at - 1.0:
            return False  # Slack handled it itself
        info = self.by_id.get(nid)
        if not info:
            print(f"slack ignored click on untracked notification {nid}", flush=True)
            return False
        params = {"team": info["teamId"], "id": info["channel"], "message": info["msg"]}
        if info.get("thread_ts"):
            params["thread_ts"] = info["thread_ts"]
        link = "slack://channel?" + urllib.parse.urlencode(params)
        print(f"slack ignored click on {nid}, opening {link}", flush=True)
        subprocess.Popen(["xdg-open", link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return False


def follow_slack_log(tracker, loop):
    """Feed Slack's NEW_NOTIFICATION / CLICK_NOTIFICATION log lines to tracker."""
    proc = subprocess.Popen(
        ["journalctl", "--user", "-f", "-n", "0", "-o", "cat", "--no-pager",
         "--identifier=slack.desktop"],
        stdout=subprocess.PIPE, text=True, bufsize=1,
    )
    block = None
    for line in proc.stdout:
        if "Store: CLICK_NOTIFICATION" in line:
            now = time.monotonic()
            GLib.idle_add(lambda: setattr(tracker, "last_click_logged", now))
        elif "Store: NEW_NOTIFICATION {" in line:
            block = ["{"]
        elif block is not None:
            block.append(line)
            if line.rstrip() == "}":
                try:
                    info = json.loads("".join(block))
                except ValueError:
                    info = {}
                block = None
                if all(info.get(k) for k in ("teamId", "channel", "msg")):
                    GLib.idle_add(lambda i=info: tracker.add_entry(i))
    print("journalctl exited", flush=True)
    GLib.idle_add(loop.quit)


def follow_notify_replies(tracker, loop):
    """Feed the ids GNOME returns for Slack's Notify calls to tracker."""
    proc = subprocess.Popen(
        ["busctl", "--user", "monitor", "--json=short",
         "--match", "type='method_call',interface='org.freedesktop.Notifications',member='Notify'",
         "--match", "type='method_return'"],
        stdout=subprocess.PIPE, text=True, bufsize=1,
    )
    pending = {}  # (caller, cookie) -> time
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("member") == "Notify":
            app, *_, hints, _timeout = msg["payload"]["data"]
            # GNOME's proxy re-sends each call to the Shell; count the original only.
            if app == "Slack" and "x-shell-sender" not in hints:
                pending[(msg["sender"], msg["cookie"])] = time.monotonic()
        elif msg.get("type") == "method_return" and pending:
            if pending.pop((msg.get("destination"), msg.get("reply_cookie")), None):
                nid = msg["payload"]["data"][0]
                GLib.idle_add(lambda: tracker.add_id(nid))
            cutoff = time.monotonic() - 10
            for k in [k for k, t in pending.items() if t < cutoff]:
                del pending[k]
    print("busctl monitor exited", flush=True)
    GLib.idle_add(loop.quit)


def main():
    tracker = NotificationTracker()

    def on_token(_conn, _sender, _path, _iface, _member, params):
        nid, token = params.unpack()
        if TOKEN_MARKER not in token:
            return
        clicked_at = time.monotonic()
        deadline = clicked_at + WAIT_FOR_WINDOW_SECS
        # Give Slack a moment to handle ActionInvoked (it may show its window).
        GLib.timeout_add(150, lambda: forward_token(token, deadline))
        GLib.timeout_add(CLICK_GRACE_MS, lambda: tracker.check_click(nid, clicked_at))

    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    bus.signal_subscribe(
        None,
        "org.freedesktop.Notifications",
        "ActivationToken",
        "/org/freedesktop/Notifications",
        None,
        Gio.DBusSignalFlags.NONE,
        on_token,
    )
    loop = GLib.MainLoop()
    threading.Thread(target=follow_notify_replies, args=(tracker, loop), daemon=True).start()
    threading.Thread(target=follow_slack_log, args=(tracker, loop), daemon=True).start()
    loop.run()
    raise SystemExit(1)


if __name__ == "__main__":
    main()
