"""Thin client for the cloudf.one REST API.

The phone API is DeviceFarmer STF 2.4.1's own /api/v1, served on cloudf.one. It
authenticates with an access token you mint yourself at cloudf.one/#!/settings.
The partner endpoints (/stock, /orders) take a separate partner key.

What the REST API cannot do, so this client does not pretend to: tap, swipe,
type, screenshot or install an APK. Those run over STF's websocket protocol or
over adb, and adb needs a WireGuard tunnel (see adb_connect below).
"""
import time
from typing import Optional

import requests

DEFAULT_BASE_URL = "https://cloudf.one/api/v1"
DEFAULT_LOCK_TIMEOUT_MS = 15 * 60 * 1000


class CloudfoneError(Exception):
    """The API answered with an error, or with something we could not read."""

    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class CloudfoneClient:
    def __init__(self, token: Optional[str] = None, partner_key: Optional[str] = None,
                 base_url: str = DEFAULT_BASE_URL, timeout: float = 30,
                 session: Optional[requests.Session] = None):
        self.token = token
        self.partner_key = partner_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "cloudfone-cli")

    # ---- plumbing -------------------------------------------------------

    def _request(self, method, path, *, key=None, **kwargs):
        key = key if key is not None else self.token
        if not key:
            raise CloudfoneError("no credential: pass a token (CLOUDFONE_TOKEN) "
                                 "or, for /stock and /orders, a partner key")
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {key}"
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, headers=headers,
                                        timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise CloudfoneError(f"{method} {path}: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400 or (isinstance(data, dict) and data.get("success") is False):
            raise CloudfoneError(_describe(method, path, resp.status_code, data),
                                 status=resp.status_code, payload=data)
        if data is None:
            raise CloudfoneError(f"{method} {path}: response was not JSON",
                                 status=resp.status_code)
        return data

    def _partner(self, method, path, **kwargs):
        if not self.partner_key:
            raise CloudfoneError("this endpoint needs a partner key (CLOUDFONE_PARTNER_KEY); "
                                 "it is issued per account on request")
        return self._request(method, path, key=self.partner_key, **kwargs)

    # ---- account and phones ---------------------------------------------

    def user(self) -> dict:
        """Your account. Doubles as a token check."""
        return self._request("GET", "/user")["user"]

    def devices(self) -> list:
        """The phones on your account (empty if the subscription has lapsed)."""
        return self._request("GET", "/devices").get("devices", [])

    def device(self, serial: str) -> dict:
        return self._request("GET", f"/devices/{serial}")["device"]

    def locks(self) -> list:
        """Phones you currently hold the control lock on."""
        return self._request("GET", "/user/devices").get("devices", [])

    def lock(self, serial: str, timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS) -> dict:
        """Take the control lock. A browser tab on the same phone takes the same
        lock, so the two will fight over it."""
        return self._request("POST", "/user/devices",
                             json={"serial": serial, "timeout": int(timeout_ms)})

    def unlock(self, serial: str) -> dict:
        return self._request("DELETE", f"/user/devices/{serial}")

    def remote_connect(self, serial: str) -> str:
        """Ask STF to open the phone's adb port. Returns "host:port".

        The host it returns (cloudf.one) does not accept raw TCP from the
        internet, so the port is only reachable through a WireGuard tunnel.
        Needs the control lock first.
        """
        return self._request("POST", f"/user/devices/{serial}/remoteConnect")["remoteConnectUrl"]

    def remote_disconnect(self, serial: str) -> dict:
        return self._request("DELETE", f"/user/devices/{serial}/remoteConnect")

    def tokens(self) -> list:
        """Titles of your access tokens (STF never returns the secrets)."""
        return self._request("GET", "/user/accessTokens").get("tokens", [])

    def add_adb_key(self, public_key: str, title: Optional[str] = None) -> dict:
        body = {"publickey": public_key.strip()}
        if title:
            body["title"] = title
        return self._request("POST", "/user/adbPublicKeys", json=body)

    def wait_ready(self, serial: Optional[str] = None, timeout: float = 300,
                   interval: float = 5, sleep=time.sleep, clock=time.monotonic) -> dict:
        """Poll /devices until a phone is present and ready, and return it.

        With no serial, the first ready phone on the account wins.
        """
        deadline = clock() + timeout
        last = None
        while True:
            phones = self.devices()
            if serial:
                phones = [d for d in phones if d.get("serial") == serial]
                if not phones:
                    raise CloudfoneError(f"{serial} is not on this account")
            for d in phones:
                if d.get("present") and d.get("ready"):
                    return d
            last = phones
            if clock() >= deadline:
                states = ", ".join(f"{d.get('serial')}: present={d.get('present')} "
                                   f"ready={d.get('ready')}" for d in (last or [])) or "no phones"
                raise CloudfoneError(f"no ready phone after {timeout:.0f}s ({states})")
            sleep(interval)

    # ---- partner (reseller) endpoints -----------------------------------

    def stock(self) -> dict:
        return self._partner("GET", "/stock")

    def orders(self) -> list:
        return self._partner("GET", "/orders").get("orders", [])

    def place_order(self, duration_days: int, quantity: int, idempotency_key: str,
                    carrier: Optional[str] = None) -> dict:
        """Buy or renew phones against the partner's prepaid balance.

        quantity is the TOTAL you want to hold, not an increment; ordering what
        you already hold renews it. Reusing idempotency_key replays the original
        order instead of buying again.
        """
        if not idempotency_key:
            raise CloudfoneError("an idempotency key is required so a retry cannot buy twice")
        body = {"duration_days": int(duration_days), "quantity": int(quantity)}
        if carrier:
            body["carrier"] = carrier
        return self._partner("POST", "/orders", json=body,
                             headers={"Idempotency-Key": idempotency_key})


def rewrite_adb_host(remote_connect_url: str, via: Optional[str]) -> str:
    """Swap the host in "host:port" for the tunnel address, keeping the port."""
    host, sep, port = remote_connect_url.rpartition(":")
    if not sep or not port.isdigit():
        raise CloudfoneError(f"unexpected remoteConnectUrl {remote_connect_url!r}")
    return f"{via}:{port}" if via else remote_connect_url


def _describe(method, path, status, data):
    detail = ""
    if isinstance(data, dict):
        detail = data.get("description") or data.get("error") or ""
        extra = {k: v for k, v in data.items()
                 if k not in ("success", "description", "error", "devices")}
        if extra and "error" in data:
            detail = f"{detail} {extra}"
    hint = {401: " (token missing, wrong or revoked)",
            403: " (not yours, already controlled, or an admin-only endpoint)",
            404: " (unknown device or endpoint)",
            504: " (the phone's host did not answer in time; try again)"}.get(status, "")
    return f"{method} {path} -> HTTP {status}{hint}: {detail}".rstrip(": ")
