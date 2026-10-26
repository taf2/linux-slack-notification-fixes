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
"""

import ctypes
import subprocess
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

TOKEN_MARKER = "/Slack/"
WAIT_FOR_WINDOW_SECS = 3.0


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


def on_signal(_conn, _sender, _path, _iface, _member, params):
    _id, token = params.unpack()
    if TOKEN_MARKER not in token:
        return
    deadline = time.monotonic() + WAIT_FOR_WINDOW_SECS
    # Give Slack a moment to handle ActionInvoked (it may show its window).
    GLib.timeout_add(150, lambda: forward_token(token, deadline))


def main():
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    bus.signal_subscribe(
        None,
        "org.freedesktop.Notifications",
        "ActivationToken",
        "/org/freedesktop/Notifications",
        None,
        Gio.DBusSignalFlags.NONE,
        on_signal,
    )
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
