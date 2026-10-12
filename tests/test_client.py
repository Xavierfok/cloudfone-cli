import json

import pytest

from cloudfone import CloudfoneClient, CloudfoneError, rewrite_adb_host
from cloudfone import cli


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Records every request and answers from a {(method, path): (status, body)} map."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.headers = {}

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        path = url.split("/api/v1", 1)[1]
        self.calls.append({"method": method, "path": path, "headers": headers or {}, **kwargs})
        answer = self.routes.get((method, path))
        if callable(answer):
            answer = answer()
        if answer is None:
            return FakeResponse(404, {"success": False, "description": "Not Found"})
        return FakeResponse(*answer)


PHONE = {"serial": "R5CT1", "model": "SM-G981B", "operator": "Singtel", "version": "16",
         "present": True, "ready": True, "battery": {"level": 80},
         "network": {"connected": True, "type": "MOBILE", "subtype": "LTE"}, "owner": None}


def client(routes, **kw):
    kw.setdefault("token", "tok")
    return CloudfoneClient(session=FakeSession(routes), **kw)


def test_sends_bearer_token_and_reads_user():
    c = client({("GET", "/user"): (200, {"success": True, "user": {"email": "a@cloudphone.local"}})})
    assert c.user()["email"] == "a@cloudphone.local"
    assert c.session.calls[0]["headers"]["Authorization"] == "Bearer tok"


def test_devices_list():
    c = client({("GET", "/devices"): (200, {"success": True, "devices": [PHONE]})})
    assert c.devices() == [PHONE]


def test_lapsed_account_returns_empty_list():
    c = client({("GET", "/devices"): (200, {"success": True, "devices": []})})
    assert c.devices() == []


def test_no_token_is_a_clear_error():
    c = CloudfoneClient(session=FakeSession({}))
    with pytest.raises(CloudfoneError, match="no credential"):
        c.user()


def test_401_explains_itself():
    c = client({("GET", "/user"): (401, {"success": False, "description": "Bad Credentials"})})
    with pytest.raises(CloudfoneError) as e:
        c.user()
    assert e.value.status == 401
    assert "revoked" in str(e.value) and "Bad Credentials" in str(e.value)


def test_success_false_with_200_is_still_an_error():
    c = client({("GET", "/user"): (200, {"success": False, "description": "nope"})})
    with pytest.raises(CloudfoneError, match="nope"):
        c.user()


def test_non_json_response_is_an_error():
    c = client({("GET", "/user"): (200, "<html>")})
    with pytest.raises(CloudfoneError, match="not JSON"):
        c.user()


def test_lock_sends_serial_and_timeout_ms():
    c = client({("POST", "/user/devices"): (200, {"success": True})})
    c.lock("R5CT1", timeout_ms=60000)
    assert c.session.calls[0]["json"] == {"serial": "R5CT1", "timeout": 60000}


def test_unlock_and_remote_connect():
    c = client({("DELETE", "/user/devices/R5CT1"): (200, {"success": True}),
                ("POST", "/user/devices/R5CT1/remoteConnect"):
                    (200, {"success": True, "remoteConnectUrl": "cloudf.one:7597"})})
    c.unlock("R5CT1")
    assert c.remote_connect("R5CT1") == "cloudf.one:7597"


def test_rewrite_adb_host_keeps_port():
    assert rewrite_adb_host("cloudf.one:7597", "10.77.0.1") == "10.77.0.1:7597"
    assert rewrite_adb_host("cloudf.one:7597", None) == "cloudf.one:7597"
    with pytest.raises(CloudfoneError):
        rewrite_adb_host("cloudf.one", "10.77.0.1")


def test_add_adb_key_strips_and_titles():
    c = client({("POST", "/user/adbPublicKeys"): (200, {"success": True})})
    c.add_adb_key("QAAAA key user@host\n", title="ci")
    assert c.session.calls[0]["json"] == {"publickey": "QAAAA key user@host", "title": "ci"}


def test_wait_ready_polls_until_ready():
    states = iter([dict(PHONE, ready=False), PHONE])
    c = client({("GET", "/devices"): lambda: (200, {"success": True, "devices": [next(states)]})})
    slept = []
    d = c.wait_ready("R5CT1", timeout=60, sleep=slept.append, clock=lambda: 0)
    assert d["serial"] == "R5CT1" and slept == [5]


def test_wait_ready_times_out_with_state():
    c = client({("GET", "/devices"):
                (200, {"success": True, "devices": [dict(PHONE, present=False)]})})
    ticks = iter([0, 100])
    with pytest.raises(CloudfoneError, match="present=False"):
        c.wait_ready(timeout=10, sleep=lambda s: None, clock=lambda: next(ticks))


def test_wait_ready_unknown_serial():
    c = client({("GET", "/devices"): (200, {"success": True, "devices": [PHONE]})})
    with pytest.raises(CloudfoneError, match="not on this account"):
        c.wait_ready("OTHER", sleep=lambda s: None)


def test_partner_endpoints_need_partner_key():
    c = client({})
    with pytest.raises(CloudfoneError, match="partner key"):
        c.stock()


def test_partner_key_is_used_not_token():
    c = client({("GET", "/stock"): (200, {"available": {"total": 3}})}, partner_key="pk")
    assert c.stock()["available"]["total"] == 3
    assert c.session.calls[0]["headers"]["Authorization"] == "Bearer pk"


def test_place_order_sends_idempotency_key():
    c = client({("POST", "/orders"): (201, {"order_id": "x", "devices": []})}, partner_key="pk")
    c.place_order(30, 2, "my-order-1", carrier="singtel")
    call = c.session.calls[0]
    assert call["headers"]["Idempotency-Key"] == "my-order-1"
    assert call["json"] == {"duration_days": 30, "quantity": 2, "carrier": "singtel"}


def test_place_order_requires_idempotency_key():
    c = client({}, partner_key="pk")
    with pytest.raises(CloudfoneError, match="idempotency"):
        c.place_order(30, 2, "")


def test_insufficient_stock_surfaces_details():
    c = client({("POST", "/orders"): (409, {"error": "insufficient_stock", "available": 1,
                                            "requested": 2})}, partner_key="pk")
    with pytest.raises(CloudfoneError, match="insufficient_stock") as e:
        c.place_order(30, 2, "k")
    assert e.value.status == 409 and "'available': 1" in str(e.value)


# ---- CLI ------------------------------------------------------------------

@pytest.fixture
def fake(monkeypatch):
    routes = {}

    def factory(**kw):
        return CloudfoneClient(session=FakeSession(routes), **kw)

    monkeypatch.setattr(cli, "CloudfoneClient", factory)
    return routes


def test_cli_devices_table(fake, capsys):
    fake[("GET", "/devices")] = (200, {"success": True, "devices": [PHONE]})
    assert cli.main(["--token", "t", "devices"]) == 0
    out = capsys.readouterr().out
    assert "R5CT1" in out and "Singtel" in out and "ready" in out and "Android 16" in out


def test_cli_json_flag_after_subcommand(fake, capsys):
    fake[("GET", "/devices")] = (200, {"success": True, "devices": [PHONE]})
    assert cli.main(["--token", "t", "devices", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["serial"] == "R5CT1"


def test_cli_error_exit_code(fake, capsys):
    fake[("GET", "/user")] = (401, {"success": False, "description": "Bad Credentials"})
    assert cli.main(["--token", "t", "whoami"]) == 1
    assert "Bad Credentials" in capsys.readouterr().err


def test_cli_order_refuses_without_yes(fake, capsys):
    assert cli.main(["--partner-key", "pk", "order", "--days", "30", "--quantity", "2",
                     "--idempotency-key", "k"]) == 1
    assert "--yes" in capsys.readouterr().err


def test_cli_adb_connect_rewrites_host_and_locks(fake, capsys):
    fake[("POST", "/user/devices")] = (200, {"success": True})
    fake[("POST", "/user/devices/R5CT1/remoteConnect")] = (
        200, {"success": True, "remoteConnectUrl": "cloudf.one:7597"})
    assert cli.main(["--token", "t", "adb-connect", "R5CT1", "--via", "10.77.0.1"]) == 0
    assert capsys.readouterr().out.strip() == "10.77.0.1:7597"


def test_cli_adb_connect_warns_without_tunnel(fake, capsys, monkeypatch):
    monkeypatch.delenv("CLOUDFONE_ADB_HOST", raising=False)
    fake[("POST", "/user/devices")] = (200, {"success": True})
    fake[("POST", "/user/devices/R5CT1/remoteConnect")] = (
        200, {"success": True, "remoteConnectUrl": "cloudf.one:7597"})
    assert cli.main(["--token", "t", "adb-connect", "R5CT1"]) == 0
    assert "not reachable from the internet" in capsys.readouterr().err


class _Proc:
    def __init__(self, out):
        self.returncode, self.stdout, self.stderr = 0, out, ""


def test_cli_adb_connect_runs_adb(fake, capsys, monkeypatch):
    fake[("POST", "/user/devices/R5CT1/remoteConnect")] = (
        200, {"success": True, "remoteConnectUrl": "cloudf.one:7597"})
    ran = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/adb")
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **kw: ran.append(cmd) or _Proc("connected to 10.77.0.1:7597"))
    assert cli.main(["--token", "t", "adb-connect", "R5CT1", "--no-lock", "--via",
                     "10.77.0.1", "--connect"]) == 0
    assert ran == [["/usr/bin/adb", "connect", "10.77.0.1:7597"]]


def test_cli_adb_connect_detects_silent_adb_failure(fake, capsys, monkeypatch):
    fake[("POST", "/user/devices/R5CT1/remoteConnect")] = (
        200, {"success": True, "remoteConnectUrl": "cloudf.one:7597"})
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/adb")
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, **kw: _Proc("failed to connect to 10.77.0.1:7597"))
    assert cli.main(["--token", "t", "adb-connect", "R5CT1", "--no-lock", "--via",
                     "10.77.0.1", "--connect"]) == 1


INSTALLED = {"success": True, "serial": "R5CT1", "package": "com.example.app", "kind": "apk", "files": 1}


def test_install_uploads_the_file_as_multipart_field_file(tmp_path):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"PK fake apk")
    c = client({("POST", "/devices/R5CT1/install"): (200, INSTALLED)})
    assert c.install("R5CT1", apk)["package"] == "com.example.app"
    call = c.session.calls[0]
    name, fh, ctype = call["files"]["file"]
    assert name == "app.apk" and ctype == "application/vnd.android.package-archive"
    assert call["headers"]["Authorization"] == "Bearer tok"


def test_install_waits_longer_than_the_default_timeout(tmp_path):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"x")
    seen = {}

    class S(FakeSession):
        def request(self, method, url, headers=None, timeout=None, **kwargs):
            seen["timeout"] = timeout
            return FakeResponse(200, INSTALLED)

    CloudfoneClient(token="tok", session=S({})).install("R5CT1", apk)
    assert seen["timeout"] >= 600


def test_install_missing_file_never_calls_the_api(tmp_path):
    c = client({})
    with pytest.raises(CloudfoneError, match="not found"):
        c.install("R5CT1", tmp_path / "nope.apk")
    assert c.session.calls == []


def test_install_refuses_files_over_the_server_cap_before_uploading(tmp_path, monkeypatch):
    from cloudfone import client as client_mod
    apk = tmp_path / "big.apk"
    apk.write_bytes(b"x" * 11)
    monkeypatch.setattr(client_mod, "MAX_INSTALL_BYTES", 10)
    c = client({})
    with pytest.raises(CloudfoneError, match="512 MB"):
        c.install("R5CT1", apk)
    assert c.session.calls == []


def test_install_busy_phone_surfaces_the_servers_reason(tmp_path):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"x")
    busy = {"success": False, "error": "install_in_progress",
            "detail": "another install is running on this phone"}
    c = client({("POST", "/devices/R5CT1/install"): (409, busy)})
    with pytest.raises(CloudfoneError) as e:
        c.install("R5CT1", apk)
    assert e.value.status == 409
    assert "another install is running" in str(e.value)


def test_cli_install_prints_the_package(fake, capsys, tmp_path):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"x")
    fake[("POST", "/devices/R5CT1/install")] = (200, INSTALLED)
    assert cli.main(["--token", "t", "install", "R5CT1", str(apk)]) == 0
    assert "installed com.example.app on R5CT1" in capsys.readouterr().out
