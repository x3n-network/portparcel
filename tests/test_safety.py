import json

import pytest

pytest_plugins = ["test_integration"]


def test_overwrite_limits_and_local_symlink(endpoint, tmp_path):
    from portparcel.service import execute

    config, local, remote, profile = endpoint
    (local / "source").write_bytes(b"new payload")
    (remote / "target").write_bytes(b"old")
    (local / "copy").write_bytes(b"preserve")

    def call(op, **kwargs):
        return execute(op, "test", config=config, **kwargs)

    assert call("upload", local_path="source", remote_path="/target", overwrite=True)["ok"]
    assert (remote / "target").read_bytes() == b"new payload"
    assert (
        call("download", remote_path="/target", local_path="copy")["error"]["code"]
        == "ALREADY_EXISTS"
    )
    assert (local / "copy").read_bytes() == b"preserve"
    assert call("download", remote_path="/target", local_path="copy", overwrite=True)["ok"]
    assert (local / "copy").read_bytes() == b"new payload"
    outside = tmp_path / "outside"
    outside.write_text("do not read")
    (local / "escape").symlink_to(outside)
    assert (
        call("upload", local_path="escape", remote_path="/escape")["error"]["code"] == "PATH_DENIED"
    )
    profile["max_bytes"] = 3
    config.write_text(json.dumps({"connections": {"test": profile}}))
    for op, args in [
        ("upload", {"local_path": "source", "remote_path": "/oversize"}),
        ("download", {"remote_path": "/target", "local_path": "too-big"}),
    ]:
        assert call(op, **args)["error"]["code"] == "LIMIT_EXCEEDED"
    assert not (remote / "oversize").exists()
    assert not (local / "too-big").exists()
    assert not list(local.glob(".portparcel-*"))
    assert not list(remote.glob(".portparcel-*"))


def test_untrusted_server_identity_rejected(endpoint):
    from portparcel.service import execute

    config, _, remote, profile = endpoint
    if profile["protocol"] == "ftp":
        pytest.skip("Plain FTP has no server identity verification")
    if profile["protocol"] == "sftp":
        known = config.parent / "untrusted_hosts"
        known.write_text("")
        profile["known_hosts"] = str(known)
    else:
        profile.pop("ca_file")
    config.write_text(json.dumps({"connections": {"test": profile}}))
    result = execute("list", "test", config=config, remote_path="/")
    assert not result["ok"], result
    assert result["error"]["code"] in ("IDENTITY_ERROR", "HOST_KEY_ERROR")
    assert not list(remote.iterdir())


def test_sftp_symlink_rejected(endpoint):
    from portparcel.service import execute

    config, _, remote, profile = endpoint
    if profile["protocol"] != "sftp":
        pytest.skip("FTP does not provide portable symlink inspection")
    (remote / "real").write_text("secret")
    (remote / "link").symlink_to("real")
    result = execute("stat", "test", config=config, remote_path="/link")
    assert result["error"]["code"] == "PATH_DENIED"


def test_bad_credentials_do_not_leak(endpoint, monkeypatch):
    from portparcel.service import execute

    config, _, _, _ = endpoint
    monkeypatch.setenv("PORTPARCEL_TEST_PASSWORD", "never-print-this-password")
    result = execute("list", "test", config=config, remote_path="/")
    assert not result["ok"]
    assert "never-print-this-password" not in json.dumps(result)


def test_non_root_remote_boundary(endpoint):
    from portparcel.service import execute

    config, _, remote, profile = endpoint
    (remote / "allowed").mkdir()
    (remote / "forbidden").write_text("private")
    profile["remote_root"] = "/allowed"
    config.write_text(json.dumps({"connections": {"test": profile}}))
    assert execute("list", "test", config=config, remote_path=".")["ok"]
    for path in ("/forbidden", "/allowed-other", "/allowed/../forbidden"):
        assert (
            execute("stat", "test", config=config, remote_path=path)["error"]["code"]
            == "PATH_DENIED"
        )
