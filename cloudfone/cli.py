"""`cloudfone` command line. Run `cloudfone --help`."""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .client import (DEFAULT_BASE_URL, CloudfoneClient, CloudfoneError,
                     rewrite_adb_host)


def _print_json(obj):
    print(json.dumps(obj, indent=2, sort_keys=True))


def _device_row(d):
    net = d.get("network") or {}
    battery = d.get("battery") or {}
    owner = (d.get("owner") or {}).get("email") or ""
    state = "ready" if d.get("present") and d.get("ready") else (
        "present" if d.get("present") else "offline")
    level = battery.get("level")
    return [d.get("serial") or "", d.get("model") or "", d.get("operator") or "",
            f"Android {d.get('version')}" if d.get("version") else "",
            net.get("subtype") or net.get("type") or "",
            f"{level}%" if level is not None else "", state,
            "locked" if owner else ""]


def _table(rows, header):
    widths = [max(len(str(r[i])) for r in rows + [header]) for i in range(len(header))]
    for r in [header] + rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())


def cmd_whoami(c, a):
    u = c.user()
    if a.json:
        return _print_json(u)
    print(f"{u.get('email')}  ({u.get('name') or 'no name'}, {u.get('privilege') or 'user'})")


def cmd_devices(c, a):
    ds = c.devices()
    if a.json:
        return _print_json(ds)
    if not ds:
        print("no phones on this account (or the subscription has lapsed)")
        return
    _table([_device_row(d) for d in ds],
           ["SERIAL", "MODEL", "CARRIER", "ANDROID", "NET", "BATTERY", "STATE", "LOCK"])


def cmd_device(c, a):
    _print_json(c.device(a.serial))


def cmd_wait(c, a):
    d = c.wait_ready(a.serial, timeout=a.timeout, interval=a.interval)
    if a.json:
        return _print_json(d)
    print(d.get("serial"))


def cmd_lock(c, a):
    c.lock(a.serial, timeout_ms=int(a.minutes * 60 * 1000))
    print(f"locked {a.serial} (auto-releases after {a.minutes:g} idle minutes)")


def cmd_unlock(c, a):
    c.unlock(a.serial)
    print(f"released {a.serial}")


def cmd_locks(c, a):
    ds = c.locks()
    if a.json:
        return _print_json(ds)
    for d in ds:
        print(d.get("serial"))


def cmd_adb_connect(c, a):
    via = a.via or os.environ.get("CLOUDFONE_ADB_HOST")
    if not a.no_lock:
        c.lock(a.serial, timeout_ms=int(a.minutes * 60 * 1000))
    url = rewrite_adb_host(c.remote_connect(a.serial), via)
    if not via:
        print("warning: no --via tunnel address given. the address STF returns is not "
              "reachable from the internet; adb needs the WireGuard tunnel "
              "(see README).", file=sys.stderr)
    if a.connect:
        adb = shutil.which("adb")
        if not adb:
            raise CloudfoneError("adb not found on PATH")
        proc = subprocess.run([adb, "connect", url], capture_output=True, text=True)
        out = (proc.stdout + proc.stderr).strip()
        print(out, file=sys.stderr)
        # adb exits 0 even when it fails to connect, so read what it said
        if proc.returncode != 0 or "connected to" not in out:
            raise CloudfoneError(f"adb connect {url} failed: {out}")
    print(url)


def cmd_adb_disconnect(c, a):
    c.remote_disconnect(a.serial)
    if not a.keep_lock:
        c.unlock(a.serial)
    print(f"closed adb on {a.serial}" + ("" if a.keep_lock else " and released the lock"))


def cmd_install(c, a):
    r = c.install(a.serial, a.file)
    if a.json:
        return _print_json(r)
    print(f"installed {r.get('package')} on {r.get('serial') or a.serial}")


def cmd_tokens(c, a):
    for t in c.tokens():
        print(t)


def cmd_add_adb_key(c, a):
    path = Path(a.key).expanduser()
    if not path.exists():
        raise CloudfoneError(f"{path} not found; run `adb start-server` once to create it")
    c.add_adb_key(path.read_text(), title=a.title)
    print(f"registered {path}")


def cmd_stock(c, a):
    _print_json(c.stock())


def cmd_orders(c, a):
    _print_json(c.orders())


def cmd_order(c, a):
    if not a.yes:
        raise CloudfoneError("this spends partner credit. re-run with --yes to confirm "
                             f"(quantity {a.quantity} is the TOTAL you will hold, not an increment)")
    _print_json(c.place_order(a.days, a.quantity, a.idempotency_key, carrier=a.carrier))


def build_parser():
    p = argparse.ArgumentParser(
        prog="cloudfone",
        description="Command line for the cloudf.one API (real Android phones in Singapore).")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--token", default=os.environ.get("CLOUDFONE_TOKEN"),
                   help="access token from cloudf.one/#!/settings (env CLOUDFONE_TOKEN)")
    p.add_argument("--partner-key", default=os.environ.get("CLOUDFONE_PARTNER_KEY"),
                   help="partner key for stock/orders (env CLOUDFONE_PARTNER_KEY)")
    p.add_argument("--base-url", default=os.environ.get("CLOUDFONE_BASE_URL", DEFAULT_BASE_URL))
    p.add_argument("--json", action="store_true", help="print raw JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="show the account behind the token").set_defaults(fn=cmd_whoami)
    sub.add_parser("devices", help="list your phones").set_defaults(fn=cmd_devices)

    s = sub.add_parser("device", help="one phone in full detail (JSON)")
    s.add_argument("serial")
    s.set_defaults(fn=cmd_device)

    s = sub.add_parser("wait", help="wait until a phone is present and ready, print its serial")
    s.add_argument("--serial")
    s.add_argument("--timeout", type=float, default=300, help="seconds (default 300)")
    s.add_argument("--interval", type=float, default=5)
    s.set_defaults(fn=cmd_wait)

    s = sub.add_parser("lock", help="take the control lock on a phone")
    s.add_argument("serial")
    s.add_argument("--minutes", type=float, default=15, help="idle auto-release (default 15)")
    s.set_defaults(fn=cmd_lock)

    s = sub.add_parser("unlock", help="release the control lock")
    s.add_argument("serial")
    s.set_defaults(fn=cmd_unlock)

    sub.add_parser("locks", help="phones you hold the lock on").set_defaults(fn=cmd_locks)

    s = sub.add_parser("adb-connect",
                       help="lock the phone, open its adb port and print host:port "
                            "(needs the WireGuard tunnel)")
    s.add_argument("serial")
    s.add_argument("--via", help="tunnel address to use instead of the host STF returns "
                                 "(env CLOUDFONE_ADB_HOST)")
    s.add_argument("--connect", action="store_true", help="also run `adb connect`")
    s.add_argument("--no-lock", action="store_true", help="you already hold the lock")
    s.add_argument("--minutes", type=float, default=15)
    s.set_defaults(fn=cmd_adb_connect)

    s = sub.add_parser("adb-disconnect", help="close the adb port and release the lock")
    s.add_argument("serial")
    s.add_argument("--keep-lock", action="store_true")
    s.set_defaults(fn=cmd_adb_disconnect)

    s = sub.add_parser("install", help="install an APK (or .apks/.xapk bundle) on one of your phones")
    s.add_argument("serial")
    s.add_argument("file", help="path to the .apk, .apks or .xapk (up to 512 MB)")
    s.set_defaults(fn=cmd_install)

    sub.add_parser("tokens", help="list your access token titles").set_defaults(fn=cmd_tokens)

    s = sub.add_parser("add-adb-key", help="register your adb public key")
    s.add_argument("--key", default="~/.android/adbkey.pub")
    s.add_argument("--title")
    s.set_defaults(fn=cmd_add_adb_key)

    sub.add_parser("stock", help="partner: what you could buy now").set_defaults(fn=cmd_stock)
    sub.add_parser("orders", help="partner: your API orders").set_defaults(fn=cmd_orders)

    s = sub.add_parser("order", help="partner: buy or renew phones (spends credit)")
    s.add_argument("--days", type=int, required=True)
    s.add_argument("--quantity", type=int, required=True, help="total phones to hold")
    s.add_argument("--idempotency-key", required=True)
    s.add_argument("--carrier")
    s.add_argument("--yes", action="store_true", help="confirm the spend")
    s.set_defaults(fn=cmd_order)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # accept --json before or after the subcommand
    want_json = "--json" in argv
    argv = [x for x in argv if x != "--json"]
    args = build_parser().parse_args(argv)
    args.json = want_json
    client = CloudfoneClient(token=args.token, partner_key=args.partner_key,
                             base_url=args.base_url)
    try:
        args.fn(client, args)
    except CloudfoneError as exc:
        print(f"cloudfone: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
