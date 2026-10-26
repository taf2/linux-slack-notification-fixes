# linux-slack-notification-fixes

Makes clicking a Slack desktop notification work on GNOME/Wayland: Slack comes
to the front, opens the conversation, and the cursor doesn't get stuck as a
busy spinner.

Tested on Ubuntu 26.04, GNOME Shell 50.1, Slack desktop 4.52.178 and 4.52.182 (`.deb`).

## The problem

Clicking a Slack notification turned the mouse cursor into a spinner for about
15 seconds and did not raise the Slack window.

## Root cause

When a notification with a `default` action is clicked, GNOME Shell:

1. creates a startup-notification / activation token
   (e.g. `gnome-shell/Slack/7648-381-host_TIME1372081825`). Mutter shows the
   busy cursor until that startup sequence completes or times out (15 s);
2. emits `ActivationToken(id, token)` on `org.freedesktop.Notifications`;
3. emits `ActionInvoked(id, "default")`.

Slack (Electron) ignores `ActivationToken`, and it sets `_NET_STARTUP_ID` on
its window as `STRING` rather than the `UTF8_STRING` Mutter requires (Mutter
logs: *"Window … has property _NET_STARTUP_ID that was expected to have type
UTF8_STRING…"*). So the token is never used: the startup sequence runs until
its timeout (the spinner), and focus-stealing prevention stops Slack from
raising its own window.

## The fix

`slack-notification-activate.py` is a small user service that does what Slack
should do with the token:

1. It listens on the session bus for `ActivationToken` signals whose token
   belongs to Slack (`…/Slack/…`).
2. It sets `_NET_STARTUP_ID` (as `UTF8_STRING`) on Slack's XWayland window
   via `xprop`. When this property changes on a mapped window, Mutter
   activates the window using the token's timestamp, so the raise is allowed.
3. It broadcasts a startup-notification `remove: ID="<token>"` message on the
   root window, which ends the sequence and stops the spinner.

Slack itself normally handles `ActionInvoked` and opens the conversation.

### Fallback for older notifications

Slack doesn't handle clicks on every notification still shown in GNOME's
tray. It appears to keep only the newest notification per conversation
clickable (a 34-minute-old one worked; an older one in the same thread
didn't), and it drops notifications for conversations you've already opened.
Such a click would raise the window but leave you wherever Slack already was.

To cover that, the helper also:

- follows Slack's log (`journalctl --user --identifier=slack.desktop`) for
  `Store: NEW_NOTIFICATION` entries, which include `teamId`, `channel`, `msg`
  and `thread_ts`;
- watches the session bus (`busctl --user monitor`) for Slack's `Notify` calls
  and the notification id GNOME returns, and pairs those with the log entries
  by time;
- if Slack hasn't logged `Store: CLICK_NOTIFICATION` within 1.5 s of a click,
  opens `slack://channel?team=…&id=…&message=…[&thread_ts=…]`, which takes
  Slack to that message or thread.

Slack must run under XWayland (`--ozone-platform=x11`) for this to work. The
included `applications/slack.desktop` overrides the system launcher to do that.

## Install

```sh
sudo apt install x11-utils python3-gi libx11-6 xdg-utils   # usually already present
./installer.sh
```

Then quit Slack completely and start it again from the app launcher, so it
runs with `--ozone-platform=x11`.

The installer:

- copies `slack-notification-activate.py` to `~/.local/bin/`
- copies `systemd/slack-notification-activate.service` to `~/.config/systemd/user/`
  and enables it (it starts with the graphical session)
- installs `applications/slack.desktop` to `~/.local/share/applications/`
  (any different existing copy is backed up to `slack.desktop.bak`)
- disables the older `slack-focus-on-request.service` workaround if present

Re-run it after pulling changes. To remove everything:

```sh
./installer.sh --uninstall
```

## Troubleshooting

```sh
journalctl --user -u slack-notification-activate -f
```

Each Slack notification should log `tracked notification <id> -> <channel>`,
and each click should log `forwarded gnome-shell/Slack/…_TIME… to 0x…`. If
Slack ignored the click, you'll also see `slack ignored click on <id>, opening slack://…`.

- **No log line on click:** the service isn't running, or GNOME didn't emit
  a token (check with
  `dbus-monitor --session "interface='org.freedesktop.Notifications'"`).
- **`no slack window`:** Slack isn't running under XWayland. Check that
  `ps -C slack -o args | grep ozone-platform=x11` shows a match.
- **Window raises but the conversation doesn't open:** look for
  `slack ignored click on untracked notification <id>`. The notification was
  sent before the helper started (or before a Slack restart), so there is
  nothing to deep-link to. Notifications that arrive afterwards are tracked.
