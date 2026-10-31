# vpn-cutoff

Cut the **live** sessions of finished 3x-ui clients — expired, over quota, or
disabled — without restarting the core.

Works on **three core types**, each cut through the lever that core actually
exposes:

| Core / inbound | How the live session is cut |
|---|---|
| **Xray** — TCP inbounds (VLESS, VMess, Trojan, Shadowsocks, xhttp/ws/tcp, behind a CDN or not) | `ss -K` on the exact 5-tuple |
| **Xray** — UDP/QUIC inbounds (Hysteria, TUIC, MASQUE) | per-flow `iptables` block keyed on the client's source port |
| **AmneziaWG** kernel inbounds | `awg set <iface> peer <pubkey> remove` |
| **WireGuard** kernel inbounds | `wg set <iface> peer <pubkey> remove` |

The 3x-ui panel removes a finished client from the running config, which stops
*new* logins. With `restartXrayOnClientDisable=false` the panel never drops the
sessions that are **already open**, so a finished user keeps using the tunnel
until their own client happens to disconnect. This tool closes that gap — and
it covers the kernel-backed WireGuard-family inbounds too, whose peers the panel
also leaves in place.

## What it does

Every 2 seconds it reads the panel DB (read-only) for clients that are
disabled, expired, or over quota. For any of them still online, it cuts the
exact session using the lever from the table above.

No restart, ever. The core process is not touched.

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
| `install-xui-cutoff.sh` | Install / upgrade. Does **not** touch the Xray log. |
| `install-xui-cutoff.sh --with-accesslog` | Install and enable the access log. |
| `install-xui-cutoff.sh --accesslog-only` | Only enable the access log. |
| `install-xui-cutoff.sh --status` | Version, service state, log path, owned rules, missing prerequisites. |
| `install-xui-cutoff.sh --uninstall` | Remove the service, files and **its own** iptables rules. |

The installer auto-detects the DB, config, Xray binary and API port, installs
any missing OS prerequisite, and gates the whole install on a built-in selftest:
if the selftest fails, nothing is started.

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
```

- `POLL` — seconds between DB checks. The DB read is cheap; subprocesses only
  run when something actually changed.
- `UDP_REJECT` — `1` blocks UDP flows with `REJECT` + ICMP instead of `DROP`.
  Helps TCP-like UDP clients react immediately; QUIC clients ignore ICMP either
  way. Set to `0` for a plain `DROP`.

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
