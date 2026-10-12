# cloudfone-cli

[![tests](https://github.com/Xavierfok/cloudfone-cli/actions/workflows/tests.yml/badge.svg)](https://github.com/Xavierfok/cloudfone-cli/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Command line and Python client for the [cloudf.one](https://cloudf.one/?utm_source=github&utm_medium=repo) API. cloudf.one rents real Android phones that sit in Singapore on local mobile SIMs, so apps see a genuine device on a Singapore carrier IP rather than an emulator or a datacenter address.

The phone panel runs on [DeviceFarmer STF](https://github.com/DeviceFarmer/stf) 2.4.1, and its REST API is exposed at `https://cloudf.one/api/v1`. This package wraps the parts of it that are scoped to your own phones, plus the partner order endpoints. The only dependency is `requests`.

## Install

```bash
pip install git+https://github.com/Xavierfok/cloudfone-cli
```

It isn't on PyPI yet, so install it from GitHub. `pip install cloudfone-cli` will work once the first release is published.

## Get a token

1. Log in at [cloudf.one](https://cloudf.one/?utm_source=github&utm_medium=repo) and open your phone once from the dashboard.
2. Go to `cloudf.one/#!/settings`, scroll to **Access tokens**, and generate one.
3. `export CLOUDFONE_TOKEN=...`

Treat the token like a password. Deleting it on the same page revokes it immediately.

## Use it

```bash
cloudfone whoami                  # token check
cloudfone devices                 # your phones: model, carrier, Android version, battery, state
cloudfone devices --json          # the raw STF device objects
cloudfone wait --timeout 300      # block until a phone is present and ready, print its serial
cloudfone lock R5CT1 --minutes 30 # take the control lock
cloudfone unlock R5CT1
cloudfone install R5CT1 app.apk   # upload and install an APK (or .apks/.xapk), prints the package
```

```text
SERIAL  MODEL     CARRIER  ANDROID     NET  BATTERY  STATE  LOCK
R5CT1   SM-G981B  Singtel  Android 16  LTE  80%      ready
```

From Python:

```python
from cloudfone import CloudfoneClient

c = CloudfoneClient(token="...")
phone = c.wait_ready(timeout=300)
print(phone["serial"], phone["operator"], phone["network"]["subtype"])
```

The control lock is the same lock a browser tab takes when you open the phone, so holding it from a script and from a tab at the same time makes them fight.

## Installing an app

APK install goes over REST, no tunnel needed:

```bash
cloudfone install R5CT1 app.apk
# installed com.example.app on R5CT1
```

It sends `POST /api/v1/devices/{serial}/install` with the file as multipart field `file` (`CloudfoneClient.install()` from Python). It takes a plain .apk or a split bundle (.apks, .xapk, or a zip of apks), installs over an existing copy, never downgrades, and doesn't pre-grant permissions. Limits: 512 MB per upload, one install per phone at a time (a second gets a 409), 30 installs an hour per account (429). On a failed install the error carries Android's own reason, such as `INSTALL_FAILED_UPDATE_INCOMPATIBLE`.

## What the API does not do

These limits come from the platform, not this package, and cloudf.one documents them on its [developers page](https://cloudf.one/developers?utm_source=github&utm_medium=repo):

- **No tap, swipe, type or screenshot over REST.** STF runs those over its websocket protocol. Drive them from the browser, or over adb.
- **adb is not open to the internet.** `remoteConnect` hands back `cloudf.one:PORT`, but that port only accepts traffic through a WireGuard tunnel. Right now the tunnel is set up by hand: email or message cloudf.one and it gets scoped to your own phone.

Once you have the tunnel, `adb-connect` does the lock, opens the port, and swaps in the tunnel address:

```bash
cloudfone add-adb-key                                  # registers ~/.android/adbkey.pub
cloudfone adb-connect R5CT1 --via TUNNEL_ADDRESS --connect
adb -s TUNNEL_ADDRESS:PORT shell getprop ro.product.model
cloudfone adb-disconnect R5CT1                         # closes the port and releases the lock
```

The first adb connection from a new key needs an on-screen "Allow USB debugging" tap, which you can do from the browser session. Some settings on the phones are enforced on a timer and revert: no screen lock, sideloading only from Play Store / Galaxy Store, Wi-Fi off. The [developers page](https://cloudf.one/developers?utm_source=github&utm_medium=repo) lists them.

## Partner endpoints

Resellers with an issued partner key can check stock and buy or renew phones against a prepaid balance:

```bash
export CLOUDFONE_PARTNER_KEY=...
cloudfone stock
cloudfone orders
cloudfone order --days 30 --quantity 2 --carrier singtel --idempotency-key order-2026-10-01 --yes
```

`--quantity` is the total you want to hold, not an increment, so ordering the number you already have renews them. Reusing an idempotency key replays the original order instead of charging again. An order the pool can't fill returns 409, and one the balance can't cover returns 402. Neither charges anything.

## Pricing

A free 24-hour trial phone when the trial pool has stock, a $5 paid test for 24 hours on the paid-only pool, or $50/month for a dedicated phone. Current details are on [cloudf.one](https://cloudf.one/?utm_source=github&utm_medium=repo).

## GitHub Actions

[cloudfone-android-test-action](https://github.com/Xavierfok/cloudfone-android-test-action) uses this package to reserve a phone for a CI job.

## License

MIT
