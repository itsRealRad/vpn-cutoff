# vpn-cutoff

**فارسی:** [README.fa.md](README.fa.md)

Cut the **live** sessions of finished 3x-ui clients — expired, over quota, or
disabled — without restarting the core.

## The two problems this solves, in order

**1. The panel restarts Xray on almost everything you do.**

3x-ui ships `restartXrayOnClientDisable=true`. With it on, the core restarts
whenever you create an inbound, create a client, edit either one, disable
either one, or when a client hits its quota or expires. Every restart drops
**every connected user** for a few seconds.

Think about what that means in practice. Your bot sells a 100 MB trial to
someone who may never become a customer — that creation disconnects all your
paying clients. That trial runs out, another disconnect. One trial sale and one
trial expiry can each knock everyone off several times a day: a stalled YouTube
or Instagram video, a lag spike and packet loss in a game, a dropped video call
or trading session. Real money, real cancellations, several times a day.

So you turn that switch off. Nothing restarts, everyone stays connected.

**2. Now a finished client never gets disconnected at all.**

The panel only removes a finished client from the *running* config. Sessions
that are **already open** are untouched, so a user whose quota ran out keeps
tunnelling traffic until their own client happens to reconnect. That is the
same bug that, during the national shutdowns, let people pull 100 GB through a
100 MB trial account and cost panel owners millions.

`vpn-cutoff` is the answer to the second problem. It reads the panel DB
(read-only), finds the clients that are finished *right now*, and cuts exactly
their live sessions — leaving everyone else connected.

So: turn the restart off, install this, and you get both properties at once.
The installer turns the switch off for you (and reports it either way).

## How each core is cut

Works on **three core types**, each cut through the lever that core actually
exposes:

| Core / inbound | How the live session is cut |
|---|---|
| **Xray** — TCP inbounds (VLESS, VMess, Trojan, Shadowsocks, xhttp/ws/tcp, behind a CDN or not) | `ss -K` on the exact 5-tuple |
| **Xray** — UDP/QUIC inbounds (Hysteria, TUIC, MASQUE) | per-flow `iptables` block keyed on the client's source port |
| **AmneziaWG** kernel inbounds | `awg set <iface> peer <pubkey> remove` |
| **WireGuard** kernel inbounds | `wg set <iface> peer <pubkey> remove` |

## What it does

Every 2 seconds it reads the panel DB (read-only) for clients that are
disabled, expired, or over quota, and cuts the live session of **every one of
them that is online** — using the lever from the table above.

This is a **state** check, not an event check: it does not wait for a client to
*cross* the boundary. A client that went over quota while offline, and only
connects hours later, is cut on the first cycle that sees it. That distinction
matters — an event-driven version passes every test you write for "user expires
while connected" and still leaks an entire client, because the client that is
already invalid never triggers the event again.

No restart, ever. The core process is not touched.

## What it deliberately does NOT do

This matters more than the feature list, because the tool kills connections:

- **never restarts Xray** — not on install, not on upgrade, not on cut. The only
  restart the installer can trigger is the one-time x-ui restart that the access
  log needs, and that is opt-in (`--with-accesslog`).
- **writes nothing to the panel DB except one setting** — its own
  `restartXrayOnClientDisable=false`. Everything else is opened read-only
  (`PRAGMA query_only=1`). No client, traffic counter or inbound is modified.
- **never kills a shared IP** — if two users sit behind one address, the per-IP
  fallback skips both rather than cutting a paying customer. Per-user cutting
  (access log) removes that limitation entirely.
- **only touches rules it owns** — iptables rules carry an `xui-cutoff` comment;
  `--uninstall` removes exactly those and nothing else.
- **stops rather than guesses** — a missing 3x-ui, a missing prerequisite it
  cannot install, or a failed selftest all abort the install with nothing
  started.

## Install

3x-ui must already be installed, with at least one inbound created. Everything
else — `python3`, `iproute2` (`ss -K`), `iptables` — is detected and installed
automatically. apt, dnf, yum, apk, pacman and zypper are all handled.

**Requires the access log for per-flow precision.** Read
[Access log](#access-log-why-it-is-required) below before choosing a mode.

```bash
curl -fsSL https://raw.githubusercontent.com/itsRealRad/vpn-cutoff/main/install-xui-cutoff.sh -o /tmp/i.sh
sudo bash /tmp/i.sh --with-accesslog
```

That is the whole procedure on a fresh host that already has the panel.

If the panel is missing the installer stops and says so:

```
  3x-ui is not installed on this host.
  Install the panel first, create one inbound, then re-run this installer.
```

A host without systemd is refused outright; there is no init fallback.

Or copy the single file and run it:

```bash
scp install-xui-cutoff.sh root@HOST:/root/
ssh root@HOST 'bash /root/install-xui-cutoff.sh --with-accesslog'
```

### Modes

| Command | Effect |
|---|---|
| `install-xui-cutoff.sh --audit` | **Start here.** Report what is leaking right now. Changes nothing, installs nothing. |
| `install-xui-cutoff.sh` | Install / upgrade. Does **not** touch the Xray log. Turns off the panel's restart switch. |
| `install-xui-cutoff.sh --with-accesslog` | Install and enable the access log. |
| `install-xui-cutoff.sh --accesslog-only` | Only enable the access log. |
| `install-xui-cutoff.sh --status` | Version, service state, log path, owned rules, missing prerequisites, restart switch. |
| `install-xui-cutoff.sh --uninstall` | Remove the service, files and **its own** iptables rules. |

The installer auto-detects the DB, config, Xray binary and API port, installs
any missing OS prerequisite, turns off `restartXrayOnClientDisable`, and gates
the whole install on a built-in selftest: if the selftest fails, nothing is
started.

## See the leak before you install

`--audit` reads the panel DB and the access log and prints what is happening on
this host right now. It never writes, never kills, never installs:

```
  vpn-cutoff audit  --  2026-10-31 20:52:26

  finished clients            : 23
     disabled                 : 3
     expired                  : 20
     over quota               : 0
  still connected RIGHT NOW   : 4 client(s), 6 live flow(s)
  quota overrun, measurable    : 12.40 GB   (~124,000 at 10,000/GB)

  email                        reason          used      limit  flows
  trial-8f21                   quota          12.51G      0.10G      2
  ali-01                       expired        84.20G   unlimited      1
  ...

  Per-user cutting is available (access log is on).
```

Two honesty notes, because a made-up number is worse than no number:

- **over quota** is exact — it is `used - limit`, straight from the DB.
- **expired / disabled** only shows *traffic used*. We cannot know how much of
  it happened after the deadline, so it is not counted as "leaked".

Set the price of a gigabyte in `/etc/xui-user-cutoff.conf`
(`PRICE_PER_GB=10000`) and the audit line becomes money. Set it to `0` to hide
the figure.

## Access log (why it is required)

Without the log the tool can only act per **IP address**: if two users share an
address (NAT, CGNAT, a CDN edge) it must skip them both, because dropping the
address would cut the valid user too.

With the access log, every accepted connection is recorded with the client's
real source address, source port **and** email:

```
from 198.51.100.24:41872 accepted tcp:... [in-example-udp >> direct] email: alice
```

That `ip:port` identifies exactly one kernel flow, so one user can be cut while
another on the same address is untouched.

Enabling it costs about **0.037 ms of CPU per connection** and ~81 bytes of disk
per line (measured with a 3000-connection A/B test on a single-core host); RAM
impact is nil. Rotation is installed for you (`/etc/logrotate.d/xray-access`,
50 MB cap, 3 rotations).

**It needs one Xray restart.** Xray binds its log path at startup, and
`xray api restartlogger` does not re-read the config. Restarting `x-ui` also
restarts Xray and — on a host that runs a tunnel (e.g. `wstunnel`) — can drop
your SSH session. Reconnect; nothing is lost.

## How precision is protected

The tool refuses to endanger a paying user:

- A per-address kill happens **only** when every user online from that address
  is invalid. Otherwise it is skipped, and the per-flow path handles it.
- Every rule it creates is tagged (`xui-cutoff`, `xui-cutoff-flow`) and only
  tagged rules are ever removed.
- Re-enabling a user releases their block on the next cycle.
- `iptables` prints a bare host as `a.b.c.d/32`; the mask is stripped when
  reading rules back, so the tool recognises its own rules and does not churn
  them.

## Configuration

`/etc/xui-user-cutoff.conf`:

```ini
DB=/etc/x-ui/x-ui.db
CONFIG=/usr/local/x-ui/bin/config.json
XRAY_BIN=/usr/local/x-ui/bin/xray-linux-amd64
API_ADDR=127.0.0.1:62789
LOG=/var/log/xui-user-cutoff.log
POLL=2
UDP_REJECT=1
PRICE_PER_GB=10000
```

- `POLL` — seconds between DB checks. The DB read is cheap; subprocesses only
  run when something actually changed.
- `UDP_REJECT` — `1` blocks UDP flows with `REJECT` + ICMP instead of `DROP`.
  Helps TCP-like UDP clients react immediately; QUIC clients ignore ICMP either
  way. Set to `0` for a plain `DROP`.
- `PRICE_PER_GB` — only used by `--audit`. Whatever unit you bill in; printed
  as-is. `0` hides the money line.

## Requirements

- 3x-ui with Xray
- Linux with `CONFIG_INET_DIAG_DESTROY=y` (`ss -K` support) and `iptables`
- `python3`
- root (it writes the firewall)

## Known limits

- **QUIC-based protocols cannot be cut instantly** from the kernel. QUIC ignores
  ICMP and does not `connect()` its UDP socket, so the client only notices when
  its own idle timeout expires. This is a property of QUIC, not a bug here.
  Shortening `udpIdleTimeout` on the inbound would shorten it, at the cost of
  disconnecting idle clients more aggressively.
- The access log is the only source of the client's source port. Xray's API does
  not expose it.
- Log lines are truncated to the last few MB when parsed, so a session older
  than that window is only reachable through the per-address path.

## License

Proprietary — free to run, no copying, no redistribution, no commercial use.
See [LICENSE](LICENSE).
