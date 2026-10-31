#!/usr/bin/env python3
"""Cut the LIVE sessions of x-ui clients that are expired / over quota / disabled.

The 3x-ui panel removes a finished client from the running Xray config, which
stops NEW connections, but with `restartXrayOnClientDisable=false` it never
drops the sockets that are already open -- so a finished user keeps using the
tunnel until their client happens to disconnect. This tool closes that gap.

Method (no restart, no restart of the core):
  1. Read the panel DB read-only and compute the INVALID client set
     (enable=0  OR  expired  OR  quota exhausted).
  2. Ask the running Xray core for its OWN per-user online IP map
     (`xray api statsonlineiplist -all`). This is authoritative: Xray tags each
     accepted connection with the email of the client that opened it, so a
     Cloudflare/CDN-fronted connection is attributed to the real user even
     though the kernel only sees the CDN edge IP.
  3. For every online IP held by an invalid user, kill the matching sockets
     with `ss -K` (source-side peer match, scoped to our own inbound ports).
     An IP that is ALSO held by a still-valid user is never killed -- it is
     logged and skipped, so a shared/NAT/CDN-edge address cannot take a good
     user down with a bad one.

Runs as a small daemon (default poll 10s). Every cycle is read-only on the DB;
the only side effect is `ss -K` on sockets already attributed to a finished
user. `--once` and `--dry-run` make it safe to exercise by hand.
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

VERSION = "1.7.0"

# How long a user we just cut is re-checked every cycle. The panel's own sweep
# that removes the client from the config runs on a ~5 min cadence, so a
# user who crossed the boundary can still reconnect for up to that long; keep a
# window slightly wider than that. Outside this window, and when the invalid set
# has not changed, the expensive subprocesses (xray api, ss, awg) are NOT run.
WATCH_SEC = 360

# config.json is read once per process and re-read only when its mtime moves.
_PORT_CACHE = {}

DEFAULTS = {
    "DB": "/etc/x-ui/x-ui.db",
    "CONFIG": "/usr/local/x-ui/bin/config.json",
    "XRAY_BIN": "/usr/local/x-ui/bin/xray-linux-amd64",
    "API_ADDR": "127.0.0.1:62789",
    "STATE_DIR": "/var/lib/xui-user-cutoff",
    "LOG": "/var/log/xui-user-cutoff.log",
    "POLL": "10",
    "SS": "ss",
    "AWG": "awg",
    "WG": "wg",
    # Block UDP flows with REJECT+ICMP instead of DROP. DROP silently blackholes,
    # so a client keeps believing it is connected until its own timeout; REJECT
    # gives an immediate hard error -- the UDP analogue of ss -K's RST. Set to 0
    # to fall back to DROP (e.g. if a client misbehaves on ICMP).
    "UDP_REJECT": "1",
}


def log(path, *parts):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(str(p) for p in parts))
    try:
        with open(path, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def load_cfg(argv):
    cfg = dict(DEFAULTS)
    path = os.environ.get("XUI_CUTOFF_CONF", "/etc/xui-user-cutoff.conf")
    if os.path.exists(path):
        with open(path) as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                k, v = raw.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"')
    for k, v in os.environ.items():
        if k.startswith("XUI_CUTOFF_"):
            cfg[k[len("XUI_CUTOFF_"):]] = v
    return cfg


def own_ports(cfg):
    """The ports our own Xray inbounds listen on (from the live config).

    Cached by config mtime: this file is read on every kill pass, and a panel
    config edit is the only thing that can change it, so re-reading 1800x/hour
    is wasted I/O.
    """
    path = cfg["CONFIG"]
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return set()
    c = _PORT_CACHE.get(path)
    if c and c[0] == mtime:
        return c[1]
    try:
        with open(path) as fh:
            d = json.load(fh)
    except Exception:
        return set()
    ports = set()
    for i in d.get("inbounds") or []:
        if i.get("protocol") == "tunnel":  # the internal api inbound
            continue
        p = i.get("port")
        if isinstance(p, int):
            ports.add(p)
        elif isinstance(p, str):
            m = re.match(r"(\d+)", p)
            if m:
                ports.add(int(m.group(1)))
    _PORT_CACHE[path] = (mtime, ports)
    return ports


def invalid_users(cfg):
    """{email: reason} for clients the panel considers finished, from the DB."""
    out = {}
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect("file:%s?mode=ro" % cfg["DB"], uri=True, timeout=5)
    try:
        con.execute("PRAGMA query_only=1")
        for email, up, down, total, expiry, enable in con.execute(
            "SELECT email, up, down, total, expiry_time, enable FROM client_traffics"
        ):
            if not enable:
                out[email] = "disabled"
            elif expiry and expiry < now_ms:
                out[email] = "expired"
            elif total and total > 0 and (up + down) >= total:
                out[email] = "quota"
    finally:
        con.close()
    return out


def online_ip_map(cfg):
    """{ip: set(email)} from the core's own per-user online list."""
    try:
        r = subprocess.run(
            [cfg["XRAY_BIN"], "api", "statsonlineiplist",
             "--server=" + cfg["API_ADDR"], "-all"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return None, "api error: %s" % e
    if r.returncode != 0:
        return None, "api rc=%d: %s" % (r.returncode, (r.stderr or "").strip()[:200])
    txt = r.stdout
    start = txt.find("{")
    if start < 0:
        return {}, None
    try:
        d = json.loads(txt[start:])
    except Exception as e:
        return None, "parse error: %s" % e
    m = {}
    for u in d.get("users") or []:
        email = u.get("email")
        for i in u.get("ips") or []:
            ip = i.get("ip")
            if ip:
                m.setdefault(ip, set()).add(email)
    return m, None


# One access-log line is written per accepted connection. Observed shapes:
#   from <ip>:<port> accepted tcp:<dest>:<dport> [in-8443-tcp >> direct] email: Mine
#   from <ip>:<port> accepted tcp:<dest>:<dport> [in-60971-udp >> direct] email: Enchantress
#   from <ip>:<port> accepted tcp:127.0.0.1:62789 [api -> api]          (no email)
#
# IMPORTANT: the `tcp:`/`udp:` right after `accepted` describes the DESTINATION
# network, not the inbound transport. A hysteria (QUIC/UDP) session logs as
# `accepted tcp:...` because the payload it forwards is TCP. The transport is
# only recoverable from the inbound TAG in the `[tag >> outbound]` part, so that
# is what decides UDP vs TCP here -- never the `tcp:` token.
ACCESS_RE = re.compile(
    r"from (udp:)?\[?([0-9a-fA-F:.]*?)\]?:(\d+) accepted "
    r"(?:tcp|udp):\S*\s*\[([^\]]*?)\s*(?:>>|->)\s*[^\]]*\]"
    r"(?:\s*email:\s*(\S+))?")

FLOW_TAG = "xui-cutoff-flow"


def inbound_transport_map(cfg):
    """{inbound_tag: 'udp'|'tcp'} from the live config, cached by mtime."""
    path = cfg["CONFIG"]
    key = ("tags", path)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {}
    c = _PORT_CACHE.get(key)
    if c and c[0] == mtime:
        return c[1]
    try:
        with open(path) as fh:
            d = json.load(fh)
    except Exception:
        return {}
    udps = udp_ports(cfg)
    out = {}
    for i in d.get("inbounds") or []:
        tag = i.get("tag")
        if not tag:
            continue
        p = _inbound_port(i)
        out[tag] = "udp" if (p in udps) else "tcp"
    _PORT_CACHE[key] = (mtime, out)
    return out


def access_log_path(cfg):
    """Path of xray's access log from the live config, or None if disabled."""
    path = cfg["CONFIG"]
    key = ("acclog", path)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    c = _PORT_CACHE.get(key)
    if c and c[0] == mtime:
        return c[1]
    try:
        with open(path) as fh:
            d = json.load(fh)
    except Exception:
        return None
    a = (d.get("log") or {}).get("access")
    res = a if (a and a not in ("none", "")) else None
    _PORT_CACHE[key] = (mtime, res)
    return res


def flow_map(cfg, max_bytes=4_000_000):
    """{(ip, port): (email, transport)} from the tail of xray's access log.

    `transport` comes from the inbound tag, not the log's `tcp:` token (see
    ACCESS_RE). Lines without an email are the panel's own API calls and are
    skipped. Reads only the last `max_bytes`, so cost does not grow with a long
    log. Returns None when the access log is disabled, so the caller can fall
    back to the coarse per-IP path.
    """
    p = access_log_path(cfg)
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return None
    tags = inbound_transport_map(cfg)
    lines = data.split(b"\n")
    if size > max_bytes:
        lines = lines[1:]           # first line is likely a partial record
    m = {}
    for raw in lines:
        mm = ACCESS_RE.search(raw.decode("utf-8", "replace"))
        if not mm or not mm.group(5):
            continue
        tag = mm.group(4)
        m[(mm.group(2), int(mm.group(3)))] = (
            mm.group(5), tags.get(tag, "tcp"))
    return m


def kill_flow(cfg, ip, port, tcp_ports):
    """Kill exactly one TCP session: peer `ip:port` into one of our TCP inbounds.

    The `dport=<peer port>` term is what makes this per-flow: another user on
    the same peer IP has a different peer port, so their socket is untouched.
    """
    killed = 0
    for p in sorted(tcp_ports):
        try:
            r = subprocess.run(
                [cfg["SS"], "-K", "dst", "=", ip, "sport", "=", ":%d" % p,
                 "dport", "=", ":%d" % port],
                capture_output=True, text=True, timeout=5)
        except Exception:
            continue
        killed += sum(1 for ln in r.stdout.splitlines() if "ESTAB" in ln)
    return killed


def existing_flow_drops(cfg):
    """{(ip, sport, dport)} of per-flow UDP DROP rules this tool owns.

    iptables normalises a bare -s host to `a.b.c.d/32`, so the mask must be
    stripped here or every cycle would see its own rules as foreign and churn
    (delete + re-add) forever.
    """
    out = set()
    try:
        r = subprocess.run(["iptables", "-S", "INPUT"], capture_output=True,
                           text=True, timeout=5)
    except Exception:
        return out
    for ln in r.stdout.splitlines():
        if FLOW_TAG not in ln:
            continue
        ip = re.search(r"-s (\S+)", ln)
        sp = re.search(r"--sport (\d+)", ln)
        dp = re.search(r"--dport (\d+)", ln)
        if ip and sp and dp:
            host = ip.group(1).split("/")[0]
            out.add((host, int(sp.group(1)), int(dp.group(1))))
    return out


def _reject_opt(cfg):
    return (["--reject-with", "icmp-port-unreachable"]
            if str(cfg.get("UDP_REJECT", "1")) not in ("0", "false", "") else [])


def flow_drop(cfg, ip, sport, dport, dry=False):
    """Block one UDP flow: this client's source port to this inbound port.

    REJECT (default) over DROP: a blackholed UDP client keeps its session until
    its own timeout, while an ICMP port-unreachable ends it immediately.
    """
    if dry:
        return False
    tgt = "REJECT" if _reject_opt(cfg) else "DROP"
    try:
        r = subprocess.run(
            ["iptables", "-I", "INPUT", "-p", "udp", "--dport", str(dport),
             "--sport", str(sport), "-s", ip, "-j", tgt] + _reject_opt(cfg) +
            ["-m", "comment", "--comment", FLOW_TAG + ("" if tgt == "DROP" else "r")],
            capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def flow_undrop(cfg, ip, sport, dport, dry=False):
    if dry:
        return False
    ok = False
    for tag in (FLOW_TAG + "r", FLOW_TAG):
        tgt = "REJECT" if tag.endswith("r") else "DROP"
        extra = (["--reject-with", "icmp-port-unreachable"]
                 if tgt == "REJECT" else [])
        try:
            r = subprocess.run(
                ["iptables", "-D", "INPUT", "-p", "udp", "--dport", str(dport),
                 "--sport", str(sport), "-s", ip, "-j", tgt] + extra +
                ["-m", "comment", "--comment", tag],
                capture_output=True, text=True, timeout=5)
            ok = ok or r.returncode == 0
        except Exception:
            continue
    return ok


def perflow_cutoff(cfg, inv, ownports, udpports, dry=False):
    """Cut exactly the flow of each invalid user, from the access log.

    TCP  -> ss -K on (peer ip, peer port), scoped to our TCP inbound ports.
    UDP  -> a per-flow iptables DROP (ip, sport -> inbound port), reconciled
            against the rules we already own so it self-heals.
    Returns None when no access log is configured (caller falls back to per-IP).
    """
    fm = flow_map(cfg)
    if fm is None:
        return None
    tcp_ports = set(ownports) - set(udpports)
    invs = set(inv)
    killed = 0
    for (ip, port), (email, proto) in fm.items():
        if proto != "tcp" or email not in invs:
            continue
        if dry:
            log(cfg["LOG"], "DRY FLOW-KILL %s:%d user=%s" % (ip, port, email))
            continue
        k = kill_flow(cfg, ip, port, tcp_ports)
        killed += k
        if k:
            log(cfg["LOG"], "FLOW-KILL %s:%d user=%s" % (ip, port, email))

    desired = set()
    for (ip, port), (email, proto) in fm.items():
        if proto == "udp" and email in invs:
            for dp in udpports:
                desired.add((ip, port, dp))
    have = existing_flow_drops(cfg)
    added = removed = 0
    for ip, sp, dp in sorted(desired - have):
        if dry:
            log(cfg["LOG"], "DRY FLOW-DROP %s:%d -> dport=%d" % (ip, sp, dp))
            continue
        if flow_drop(cfg, ip, sp, dp, dry=False):
            added += 1
            log(cfg["LOG"], "FLOW-DROP %s:%d -> dport=%d" % (ip, sp, dp))
    for ip, sp, dp in sorted(have - desired):
        if dry:
            log(cfg["LOG"], "DRY FLOW-UNDROP %s:%d -> dport=%d" % (ip, sp, dp))
            continue
        if flow_undrop(cfg, ip, sp, dp, dry=False):
            removed += 1
            log(cfg["LOG"], "FLOW-UNDROP %s:%d -> dport=%d" % (ip, sp, dp))
    return {"killed": killed, "added": added, "removed": removed}


def kill_ip(cfg, ip, ports):
    """Kill every socket whose peer is `ip` on one of our inbound ports.

    ss -K filter semantics: src/dport refer to the local side, dst/sport to the
    peer... verified on this host -- `dst=<peer> sport=:<ourport>` matches the
    server-side socket exactly, so a CDN-edge peer IP plus our listening port
    can only ever match a connection into our own inbound.
    """
    killed = 0
    for p in sorted(ports):
        for args in (["-K", "dst", "=", ip, "sport", "=", ":%d" % p],
                     ["-K", "-u", "dst", "=", ip, "sport", "=", ":%d" % p]):
            try:
                r = subprocess.run([cfg["SS"]] + args, capture_output=True,
                                   text=True, timeout=5)
            except Exception:
                continue
            killed += sum(1 for ln in r.stdout.splitlines() if "ESTAB" in ln or "UNCONN" in ln)
    return killed


# Inbound protocols/networks that are UDP-based: they MUST be handled by an
# iptables DROP (no per-peer kernel socket exists), never by ss -K.
UDP_PROTOCOLS = {"hysteria", "hysteria2", "tuic", "masque"}
# QUIC/KCP based transports carry UDP regardless of the proxy protocol name.
UDP_NETWORKS = {"hysteria", "quic", "kcp", "mkcp", "masque", "masque2"}
# Proxy protocols that are definitely TCP; when a protocol is NOT in either set
# we fall back to the network test. Being explicit about the known-TCP set means
# a new UDP protocol is not silently mishandled: unknown protocol + unknown
# network => treated as TCP (ss -K), which is the safe default because ss -K on
# a UDP port simply matches nothing (a no-op), whereas a wrong DROP could cut
# a valid user.
TCP_PROTOCOLS = {"vless", "vmess", "trojan", "shadowsocks", "socks", "http",
                 "dokodemo-door", "wireguard"}


def _inbound_port(i):
    p = i.get("port")
    if isinstance(p, int):
        return p
    if isinstance(p, str):
        m = re.match(r"(\d+)", p)
        if m:
            return int(m.group(1))
    return None


def udp_ports(cfg):
    """Ports whose inbound speaks UDP (hysteria/tuic/masque, or any quic/kcp).

    Used to decide where a kernel DROP is needed -- `ss -K` cannot kill a UDP
    session because the kernel has no per-peer UDP socket, so an iptables DROP
    is the only lever. Cached by config mtime like own_ports.
    """
    path = cfg["CONFIG"]
    key = ("udp", path)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return set()
    c = _PORT_CACHE.get(key)
    if c and c[0] == mtime:
        return c[1]
    try:
        with open(path) as fh:
            d = json.load(fh)
    except Exception:
        return set()
    ports = set()
    for i in d.get("inbounds") or []:
        if i.get("protocol") == "tunnel":
            continue
        proto = (i.get("protocol") or "").lower()
        net = ((i.get("streamSettings") or {}).get("network") or "").lower()
        is_udp = (proto in UDP_PROTOCOLS) or (net in UDP_NETWORKS)
        if proto in TCP_PROTOCOLS and net not in UDP_NETWORKS:
            is_udp = False
        if is_udp:
            p = _inbound_port(i)
            if p is not None:
                ports.add(p)
    _PORT_CACHE[key] = (mtime, ports)
    return ports


DROP_TAG = "xui-cutoff"


def existing_drops(cfg):
    """{ip: [ports]} of DROP rules this tool owns (recognised by its comment).

    The /32 mask iptables adds to a bare -s host is stripped, or every cycle
    would treat its own rules as foreign (churning them).
    """
    out = {}
    try:
        r = subprocess.run(["iptables", "-S", "INPUT"], capture_output=True,
                           text=True, timeout=5)
    except Exception:
        return out
    for ln in r.stdout.splitlines():
        if DROP_TAG not in ln or FLOW_TAG in ln:
            continue
        m = re.search(r"-s (\S+)", ln)
        p = re.search(r"--dport (\d+)", ln)
        if m and p:
            out.setdefault(m.group(1).split("/")[0], []).append(int(p.group(1)))
    return out


def drop_ip(cfg, ip, port, dry=False):
    """Install a DROP for (peer ip, udp, our port). Idempotent by comment tag."""
    if dry:
        return False
    try:
        r = subprocess.run(
            ["iptables", "-I", "INPUT", "-p", "udp", "--dport", str(port),
             "-s", ip, "-j", "DROP", "-m", "comment", "--comment", DROP_TAG],
            capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def undrop_ip(cfg, ip, port, dry=False):
    if dry:
        return False
    try:
        r = subprocess.run(
            ["iptables", "-D", "INPUT", "-p", "udp", "--dport", str(port),
             "-s", ip, "-j", "DROP", "-m", "comment", "--comment", DROP_TAG],
            capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def udp_cutoff(cfg, inv, online_map, dry=False):
    """Block UDP sessions of invalid users -- ONLY when the IP is exclusive.

    A UDP session has no per-peer socket, so `ss -K` cannot touch it and the
    only kernel lever is an iptables DROP. A DROP is per-ADDRESS, not per
    session: if a still-valid user shares that address (shared NAT, CGNAT,
    a CDN edge), dropping it would cut them too. So we drop ONLY an address
    whose every online holder is invalid; any shared address is skipped and
    logged. This is the conservative rule the owner chose.
    """
    ports = udp_ports(cfg)
    if not ports:
        return {"added": 0, "removed": 0, "skipped": 0}
    have = existing_drops(cfg)
    added = removed = skipped = 0

    # 1) release DROPs that are no longer exclusively-invalid: either a valid
    #    user now holds the address, or nobody is online from it (so there is no
    #    session to block, and the rule is stale)
    for ip, dports in have.items():
        holders = online_map.get(ip) or set()
        if not (holders and holders <= set(inv)):
            for p in dports:
                if undrop_ip(cfg, ip, p, dry=dry):
                    removed += 1
                    why = ("valid user present: " + ",".join(sorted(holders - set(inv)))) \
                        if holders else "no live holder"
                    log(cfg["LOG"], "UNDROP ip=%s port=%s (%s)" % (ip, p, why))

    # 2) add DROPs for addresses that are exclusively invalid
    for ip, emails in online_map.items():
        if not (emails & set(inv)):
            continue
        if emails <= set(inv):
            for p in sorted(ports):
                if p in (have.get(ip) or []):
                    continue
                if drop_ip(cfg, ip, p, dry=dry):
                    added += 1
                    log(cfg["LOG"], "DROP ip=%s port=%s users=%s" % (
                        ip, p, ",".join(sorted(emails))))
        else:
            skipped += 1
            log(cfg["LOG"], "UDP-SKIP ip=%s shared-with-valid=%s (invalid=%s)" % (
                ip, ",".join(sorted(emails - set(inv))),
                ",".join(sorted(emails & set(inv)))))
    return {"added": added, "removed": removed, "skipped": skipped}


def live_peers(tool, iface):
    try:
        r = subprocess.run([tool, "show", iface, "peers"],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return set(x.strip() for x in r.stdout.split() if x.strip())


# Kernel WireGuard-family inbounds and the userspace tool that manages each.
# Both keep peers in the kernel, invisible to the Xray online list, so they need
# their own cutoff path. The interface name is derived from the inbound id.
WG_FAMILY = {"amneziawg": ("awg", "xui-awg%d"), "wireguard": ("wg", "xui-wg%d")}


def wg_cutoff(cfg, invalid, dry=False):
    """Remove kernel-WireGuard peers of invalid clients (amneziawg AND wg).

    AmneziaWG/WireGuard inbounds live in the kernel as `xui-awg<id>` / `xui-wg<id>`
    interfaces; their peers are NOT in the Xray statsonlineiplist, so they need
    their own path. Map email -> publicKey from the inbound settings, then
    remove that peer. Only peers actually present are touched, so an
    already-removed peer is not re-logged every cycle (the `... peer remove`
    call is idempotent). The kernel is the source of truth for "is this peer
    live", not the panel's DB, which keeps the client row forever.
    """
    try:
        con = sqlite3.connect("file:%s?mode=ro" % cfg["DB"], uri=True, timeout=5)
        con.execute("PRAGMA query_only=1")
        rows = list(con.execute(
            "SELECT id, protocol, settings FROM inbounds "
            "WHERE protocol IN ('amneziawg','wireguard') AND enable=1"))
        con.close()
    except Exception:
        return 0
    removed = 0
    for iid, proto, settings in rows:
        tool, tpl = WG_FAMILY.get((proto or "").lower(), (None, None))
        if not tool:
            continue
        iface = tpl % iid
        try:
            st = json.loads(settings or "{}")
        except Exception:
            continue
        live = live_peers(cfg.get(tool.upper()) or tool, iface)
        if live is None:
            continue
        for c in st.get("clients") or []:
            email = c.get("email")
            pk = c.get("publicKey")
            if not (email in invalid and pk and pk in live):
                continue
            if dry:
                log(cfg["LOG"], "DRY %s remove %s peer=%s iface=%s" % (tool, email, pk[:12], iface))
                removed += 1
                continue
            try:
                r = subprocess.run([cfg.get(tool.upper()) or tool, "set", iface, "peer", pk, "remove"],
                                   capture_output=True, text=True, timeout=5)
            except Exception as e:
                log(cfg["LOG"], "%s error iface=%s: %s" % (tool, iface, e))
                continue
            if r.returncode == 0:
                removed += 1
                log(cfg["LOG"], "%s-CUT email=%s iface=%s peer=%s" % (tool.upper(), email, iface, pk[:12]))
    return removed


def one_cycle(cfg, dry=False, state=None):
    """Cheap DB read every cycle; expensive subprocesses only when needed.

    The poll exists to notice a user crossing a boundary, not to run the kill
    machinery. Read the invalid set (a local SQLite query, no subprocess); if it
    is unchanged AND no recently-cut user is still in the watch window, there is
    nothing to do and no subprocess runs. That is the common case: on a box where
    nobody expires for an hour, an hour of polls costs only the DB reads.
    """
    inv = invalid_users(cfg)
    now = time.time()
    if state is None:
        state = {"prev_inv": None, "watch": {}}

    # a first cycle (prev None) counts as changed, so pre-existing invalid users
    # that may still hold live sockets are cleaned once at startup
    changed = state["prev_inv"] != inv
    watch = {e: t for e, t in state.get("watch", {}).items() if now - t < WATCH_SEC}
    if state["prev_inv"] is not None:
        # only a user that JUST became invalid is worth re-checking; on the first
        # cycle every invalid user looks "new" but nothing actually happened now
        for e in inv:
            if e not in state["prev_inv"]:
                watch[e] = now
    state["watch"] = watch
    state["prev_inv"] = dict(inv)

    if not inv:
        # nothing is invalid -- but a DROP we installed earlier might still be
        # sitting there (user was re-enabled). Release any orphan, and reconcile
        # any per-flow rules down to the (now empty) desired set.
        udp = udp_cutoff(cfg, inv, {}, dry=dry) if existing_drops(cfg) else \
            {"added": 0, "removed": 0, "skipped": 0}
        perflow = None
        if access_log_path(cfg) is not None and existing_flow_drops(cfg):
            perflow = perflow_cutoff(cfg, {}, own_ports(cfg), udp_ports(cfg), dry=dry)
        return {"invalid": 0, "killed": 0, "skipped": 0, "awg": 0, "udp": udp,
                "perflow": perflow, "reason": None,
                "ran": bool(udp.get("removed") or (perflow and perflow.get("removed")))}
    if not changed and not watch:
        return {"invalid": len(inv), "killed": 0, "skipped": 0, "awg": 0,
                "udp": {"added": 0, "removed": 0, "skipped": 0},
                "reason": None, "ran": False}

    m, err = online_ip_map(cfg)
    if m is None:
        return {"invalid": len(inv), "killed": 0, "skipped": 0, "reason": err,
                "ran": True, "error": True}

    ports = own_ports(cfg)
    udps = udp_ports(cfg)
    # An IP is safe to kill only when EVERY user holding it is invalid.
    kills = {}
    skipped = []
    for ip, emails in m.items():
        if not (emails & set(inv)):
            continue
        if emails <= set(inv):
            kills[ip] = sorted(emails)
        else:
            skipped.append((ip, sorted(emails), sorted(emails - set(inv))))

    killed = 0
    if not dry:
        for ip in kills:
            k = kill_ip(cfg, ip, ports)
            killed += k
            if k:
                log(cfg["LOG"], "KILL ip=%s users=%s sockets=%d" % (ip, ", ".join(kills[ip]), k))
    for ip, allu, valid in skipped:
        log(cfg["LOG"], "SKIP ip=%s shared-with-safe=%s — trying per-flow" % (
            ip, ", ".join(valid)))

    # Per-flow cutoff: when xray's access log is on, the (client ip, client port)
    # of each accepted connection is available, so an invalid user can be cut
    # even when their address is SHARED with a valid user (the case the per-IP
    # pass above must skip). Additive to the per-IP pass: the per-IP pass still
    # covers flows too old to be in the log tail, but only for exclusive addresses.
    perflow = perflow_cutoff(cfg, inv, ports, udps, dry=dry)

    # Kernel-backed AmneziaWG/WireGuard peers are invisible to the Xray online list.
    awg = wg_cutoff(cfg, inv, dry=dry)
    # UDP inbounds (hysteria) have no per-peer socket: only an iptables DROP
    # can stop them, and only for an address no valid user shares.
    udp = udp_cutoff(cfg, inv, m, dry=dry)
    out = {"invalid": len(inv), "ips": len(kills), "killed": killed,
           "skipped": len(skipped), "awg": awg, "udp": udp,
           "reason": None, "ran": True}
    if perflow is not None:
        out["perflow"] = perflow
        out["killed"] = killed + perflow.get("killed", 0)
    return out


def main():
    ap = argparse.ArgumentParser(description="Cut live sessions of finished x-ui clients")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--dry-run", action="store_true", help="report, kill nothing")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--version", action="version", version=VERSION)
    a = ap.parse_args()
    cfg = load_cfg(None)

    if a.selftest:
        return selftest(cfg)

    os.makedirs(cfg["STATE_DIR"], exist_ok=True)
    if a.once or a.dry_run:
        res = one_cycle(cfg, dry=a.dry_run)
        log(cfg["LOG"], "cycle once dry=%s %s" % (a.dry_run, json.dumps(res)))
        return 0

    log(cfg["LOG"], "start v%s poll=%ss db=%s" % (VERSION, cfg["POLL"], cfg["DB"]))
    state = {"prev_inv": None, "watch": {}}
    while True:
        try:
            r = one_cycle(cfg, state=state)
            if r.get("ran"):
                log(cfg["LOG"], "cycle %s" % json.dumps(r))
        except Exception as e:
            log(cfg["LOG"], "cycle error: %s" % e)
        time.sleep(max(1, int(cfg["POLL"])))


def selftest(cfg):
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            fails += 1

    # kill filter: a peer IP on our port must match, a peer IP on another port must not
    import socket
    import threading
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 45077))
    srv.listen(1)

    def accept():
        try:
            c, _ = srv.accept()
            time.sleep(4)
        except OSError:
            pass
    threading.Thread(target=accept, daemon=True).start()
    time.sleep(0.3)
    cli = socket.socket()
    cli.bind(("127.0.0.2", 0))
    cli.connect(("127.0.0.1", 45077))
    time.sleep(0.4)
    r = subprocess.run(["ss", "-K", "dst", "=", "127.0.0.2", "sport", "=", ":45077"],
                       capture_output=True, text=True)
    check("kill matches our-port peer", "ESTAB" in r.stdout)
    time.sleep(0.3)
    try:
        cli.settimeout(1)
        cli.send(b"x")
        cli.recv(10)
        dead = False
    except Exception:
        dead = True
    check("matched socket is really closed", dead)
    cli.close()
    srv.close()

    # invalid-user computation on a synthetic DB
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "t.db")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE client_traffics(id INTEGER,email TEXT,up INT,down INT,total INT,expiry_time INT,enable INT)")
        now = int(time.time() * 1000)
        con.executemany("INSERT INTO client_traffics VALUES (?,?,?,?,?,?,?)", [
            (1, "good", 10, 10, 1000, now + 86400000, 1),
            (2, "off", 10, 10, 0, 0, 0),
            (3, "exp", 10, 10, 0, now - 1000, 1),
            (4, "over", 600, 500, 1000, now + 86400000, 1),
            (5, "unlim", 10 ** 12, 0, 0, 0, 1),
        ])
        con.commit()
        con.close()
        c2 = dict(cfg)
        c2["DB"] = db
        inv = invalid_users(c2)
        check("disabled is invalid", inv.get("off") == "disabled")
        check("expired is invalid", inv.get("exp") == "expired")
        check("over-quota is invalid", inv.get("over") == "quota")
        check("good client is NOT invalid", "good" not in inv)
        check("unlimited is NOT invalid", "unlim" not in inv)

    # --- protocol -> UDP/TCP classification (drives which cutoff path runs) ---
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cfgp = dict(cfg)
        cfgp["CONFIG"] = os.path.join(td, "config.json")
        _PORT_CACHE.clear()
        json.dump({"inbounds": [
            {"protocol": "hysteria", "port": 1001, "streamSettings": {"network": "hysteria"}},
            {"protocol": "tuic", "port": 1002, "streamSettings": {"network": "quic"}},
            {"protocol": "masque", "port": 1003, "streamSettings": {}},
            {"protocol": "wireguard", "port": 1004, "streamSettings": {}},
            {"protocol": "vless", "port": 1005, "streamSettings": {"network": "xhttp"}},
            {"protocol": "vless", "port": 1006, "streamSettings": {"network": "tcp"}},
            {"protocol": "shadowsocks", "port": 1007, "streamSettings": {"network": "tcp"}},
            {"protocol": "vmess", "port": 1008, "streamSettings": {"network": "ws"}},
            {"protocol": "tunnel", "port": 1009, "streamSettings": {}},
        ]}, open(cfgp["CONFIG"], "w"))
        u = udp_ports(cfgp)
        check("classify: hysteria/tuic/masque are UDP",
              {1001, 1002, 1003} <= u)
        check("classify: vless/vmess/ss are NOT UDP",
              not ({1005, 1006, 1007, 1008} & u))
        check("classify: internal 'tunnel' api inbound is excluded", 1009 not in u)
        _PORT_CACHE.clear()

    # --- UDP cutoff decision logic (rule layer mocked, no iptables touched) ---
    cfg3 = dict(cfg)
    cfg3["LOG"] = "/dev/null"
    orig = (udp_ports, existing_drops, drop_ip, undrop_ip)
    rules = set()                     # (ip, port)

    globals()["udp_ports"] = lambda c: {60971}
    globals()["existing_drops"] = lambda c: _group(rules)

    def _drop(c, ip, p, dry=False):
        rules.add((ip, p))
        return True

    def _undrop(c, ip, p, dry=False):
        rules.discard((ip, p))
        return True

    globals()["drop_ip"] = _drop
    globals()["undrop_ip"] = _undrop
    try:
        # exclusive invalid ip -> dropped
        r = udp_cutoff(cfg3, {"bad": "quota"}, {"1.1.1.1": {"bad"}}, dry=False)
        check("udp: exclusive-invalid ip gets a DROP",
              (("1.1.1.1", 60971) in rules) and r["added"] == 1)
        # shared ip -> skipped
        r = udp_cutoff(cfg3, {"bad": "quota"}, {"2.2.2.2": {"bad", "good"}}, dry=False)
        check("udp: shared-with-valid ip is SKIPPED",
              (("2.2.2.2", 60971) not in rules) and r["skipped"] == 1)
        # the invalid user is re-enabled -> the DROP must be released
        rules.add(("1.1.1.1", 60971))   # (a stale rule was already reaped above)
        r = udp_cutoff(cfg3, {}, {"1.1.1.1": {"bad"}}, dry=False)
        check("udp: released when the user becomes valid",
              (("1.1.1.1", 60971) not in rules) and r["removed"] == 1)
        # stale rule with no live holder -> released
        rules.add(("3.3.3.3", 60971))
        udp_cutoff(cfg3, {"bad": "quota"}, {}, dry=False)
        check("udp: stale rule with no holder is released",
              ("3.3.3.3", 60971) not in rules)
    finally:
        for name, fn in zip(("udp_ports", "existing_drops", "drop_ip", "undrop_ip"), orig):
            globals()[name] = fn

    # --- access-log parsing: the (ip, port) -> email map that enables per-flow ---
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cfg4 = dict(cfg)
        cfg4["CONFIG"] = os.path.join(td, "config.json")
        logp = os.path.join(td, "access.log")
        json.dump({"log": {"access": logp, "loglevel": "info"}, "inbounds": [
            {"tag": "in-8443-tcp", "port": 8443, "protocol": "vless",
             "streamSettings": {"network": "xhttp"}},
            {"tag": "in-60971-udp", "port": 60971, "protocol": "hysteria",
             "streamSettings": {"network": "hysteria"}},
            {"tag": "api", "port": 62789, "protocol": "tunnel"},
        ]}, open(cfg4["CONFIG"], "w"))
        _PORT_CACHE.clear()
        # EXACT shapes seen in production, incl. the trap: a hysteria (UDP)
        # session whose line says `accepted tcp:` (dest network, not transport).
        with open(logp, "w") as fh:
            fh.write("2026/10/31 04:09:28.045319 from 89.34.49.116:35027 accepted tcp:example.com:443 [in-8443-tcp >> direct] email: Mine\n")
            fh.write("2026/10/31 04:38:24.238421 from 5.201.217.97:61688 accepted tcp:57.144.244.192:443 [in-60971-udp >> direct] email: Enchantress\n")
            fh.write("2026/10/31 04:09:30.2 from 89.34.49.116:35028 accepted tcp:other.com:443 [in-8443-tcp >> direct] email: Linux-homeserver\n")
            fh.write("2026/10/31 04:45:13.004027 from 127.0.0.1:11228 accepted tcp:127.0.0.1:62789 [api -> api]\n")
        fm = flow_map(cfg4)
        check("flow: tcp line parsed with port+email",
              fm.get(("89.34.49.116", 35027)) == ("Mine", "tcp"))
        check("flow: hysteria line is UDP despite 'accepted tcp:' token",
              fm.get(("5.201.217.97", 61688)) == ("Enchantress", "udp"))
        check("flow: two users on one IP are separate flows",
              fm.get(("89.34.49.116", 35028)) == ("Linux-homeserver", "tcp"))
        check("flow: api line (no email) ignored", len(fm) == 3)

        # the shared-IP case that the per-IP pass must skip is solved here:
        # only the invalid user's port is targeted, never the whole address
        inv4 = {"Mine": "disabled"}
        calls = []
        orig_kf = globals()["kill_flow"]
        orig_efd = globals()["existing_flow_drops"]
        orig_fd = globals()["flow_drop"]
        orig_fu = globals()["flow_undrop"]
        orig_log = cfg4["LOG"]
        globals()["kill_flow"] = lambda c, ip, port, tcp_ports: (
            calls.append((ip, port)) or 1)
        globals()["existing_flow_drops"] = lambda c: set()
        globals()["flow_drop"] = lambda c, ip, sp, dp, dry=False: (
            calls.append((ip, sp, "udp", dp)) or True)
        globals()["flow_undrop"] = lambda c, ip, sp, dp, dry=False: True
        cfg4["LOG"] = "/dev/null"
        try:
            r = perflow_cutoff(cfg4, inv4, {8443, 80, 60971}, {60971}, dry=False)
            check("perflow: only the invalid user's tcp flow is killed",
                  (("89.34.49.116", 35027) in calls) and
                  (("89.34.49.116", 35028) not in calls))
            check("perflow: shared-ip valid user untouched",
                  not any(c[1] == 35028 for c in calls))
            check("perflow: valid user's udp flow not dropped",
                  not any(c[0] == "5.201.217.97" for c in calls))
        finally:
            globals()["kill_flow"] = orig_kf
            globals()["existing_flow_drops"] = orig_efd
            globals()["flow_drop"] = orig_fd
            globals()["flow_undrop"] = orig_fu
            cfg4["LOG"] = orig_log

        # a hysteria user who IS invalid must get a per-flow UDP drop
        calls2 = []
        globals()["kill_flow"] = lambda c, ip, port, tcp_ports: 0
        globals()["existing_flow_drops"] = lambda c: set()
        globals()["flow_drop"] = lambda c, ip, sp, dp, dry=False: (
            calls2.append((ip, sp, dp)) or True)
        globals()["flow_undrop"] = lambda c, ip, sp, dp, dry=False: True
        cfg4["LOG"] = "/dev/null"
        try:
            r = perflow_cutoff(cfg4, {"Enchantress": "quota"}, {8443, 60971},
                               {60971}, dry=False)
            check("perflow: invalid hysteria user gets per-flow UDP drop",
                  ("5.201.217.97", 61688, 60971) in calls2)
        finally:
            globals()["kill_flow"] = orig_kf
            globals()["existing_flow_drops"] = orig_efd
            globals()["flow_drop"] = orig_fd
            globals()["flow_undrop"] = orig_fu
            cfg4["LOG"] = orig_log

        # no access log configured -> None, caller falls back to per-IP
        cfg4["CONFIG"] = os.path.join(td, "off.json")
        json.dump({"log": {"access": "none"}, "inbounds": []}, open(cfg4["CONFIG"], "w"))
        _PORT_CACHE.clear()
        check("perflow: returns None when access log is off",
              perflow_cutoff(cfg4, inv4, {80}, {60971}, dry=True) is None)
        _PORT_CACHE.clear()

    # --- existing_flow_drops must strip iptables' /32 (or it churns forever) ---
    orig_run = subprocess.run
    FAKE = ("-A INPUT -s 212.80.12.162/32 -p udp -m udp --sport 54648 "
            "--dport 60971 -m comment --comment xui-cutoff-flowr -j REJECT "
            "--reject-with icmp-port-unreachable\n"
            "-A INPUT -s 212.80.12.162/32 -p udp -m udp --sport 9999 "
            "--dport 60971 -m comment --comment xui-cutoff-flow -j DROP\n")
    subprocess.run = lambda *a, **k: type("R", (), {"stdout": FAKE,
                                                    "returncode": 0})()
    try:
        have = existing_flow_drops(cfg)
        check("drops: /32 stripped so rules are recognised (no churn)",
              ("212.80.12.162", 54648, 60971) in have and
              ("212.80.12.162", 9999, 60971) in have and
              not any("/32" in h[0] for h in have))
    finally:
        subprocess.run = orig_run

    print("SELFTEST %s" % ("ALL PASS" if not fails else "%d FAIL" % fails))
    return 0 if not fails else 1


def _group(rules):
    out = {}
    for ip, p in rules:
        out.setdefault(ip, []).append(p)
    return out


if __name__ == "__main__":
    sys.exit(main())
