#!/usr/bin/env python3
"""Turn xray's ACCESS LOG on/off WITHOUT restarting the core.

The panel re-renders /usr/local/x-ui/bin/config.json from the DB template
(`settings.xrayTemplateConfig`) on every client save, so editing the file alone
is transient. This edits BOTH the DB template and the live config.json, then
asks the running core to reopen its log files via `xray api restartlogger`
(which does NOT restart the process).

  --show     report current settings (template, live file, API reachable)
  --on       enable  access log -> /var/log/xray/access.log, loglevel=info
  --off      disable access log -> none, loglevel=warning
"""
import argparse, json, os, shutil, sqlite3, subprocess, sys, time

DB = "/etc/x-ui/x-ui.db"
CFG = "/usr/local/x-ui/bin/config.json"
XRAY = "/usr/local/x-ui/bin/xray-linux-amd64"
API = "127.0.0.1:62789"
LOGP = "/var/log/xray/access.log"
BKPDB = "/root/xui-db-accesslog-backup.db"
BKPJSON = "/root/xui-configjson-accesslog-backup.json"


def restart_logger():
    r = subprocess.run([XRAY, "api", "restartlogger", "--server=" + API],
                       capture_output=True, text=True, timeout=8)
    return r.returncode == 0


def read_template(c):
    row = c.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()
    return json.loads(row[0]) if row else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--on", action="store_true")
    ap.add_argument("--off", action="store_true")
    a = ap.parse_args()

    c = sqlite3.connect(DB)
    tmpl = read_template(c)
    tl = tmpl.get("log") or {}
    cl = (json.load(open(CFG)).get("log") or {})
    print("template.log :", json.dumps(tl))
    print("livefile.log :", json.dumps(cl))
    print("api pid      :", subprocess.run(
        ["sh", "-c", "ss -tlnp 2>/dev/null | grep -o 'pid=[0-9]*' | head -1"],
        capture_output=True, text=True).stdout.strip() or "?")

    if a.show or not (a.on or a.off):
        return

    if not os.path.exists(BKPDB):
        shutil.copy(DB, BKPDB)
        print("db backup   ->", BKPDB)
    if not os.path.exists(BKPJSON):
        shutil.copy(CFG, BKPJSON)
        print("cfg backup  ->", BKPJSON)

    if a.on:
        os.makedirs(os.path.dirname(LOGP), exist_ok=True)
        tl["access"] = LOGP
        tl["loglevel"] = "info"
        cl["access"] = LOGP
        cl["loglevel"] = "info"
    else:
        tl["access"] = "none"
        tl["loglevel"] = "warning"
        cl["access"] = "none"
        cl["loglevel"] = "warning"

    tmpl["log"] = tl
    c.execute("UPDATE settings SET value=? WHERE key='xrayTemplateConfig'",
              (json.dumps(tmpl, separators=(",", ":")),))
    c.commit()
    c.close()

    full = json.load(open(CFG))
    full["log"] = cl
    tmp = CFG + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(full, fh, separators=(",", ":"))
    os.replace(tmp, CFG)

    ok = restart_logger()
    print("template+file updated; restartlogger rc=%s" % ("0 (ok)" if ok else "FAIL"))
    print("verify:")
    print("  livefile.log:", json.dumps((json.load(open(CFG)).get("log") or {})))
    if a.on:
        print("  log file exists:", os.path.exists(LOGP))
    sys.exit(0)


main()
