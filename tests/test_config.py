import json

import pytest


def test_named_profile_is_safe_by_default(tmp_path):
    from portparcel.config import load_profile

    config = tmp_path / "connections.json"
    config.write_text(
        json.dumps(
            {
                "connections": {
                    "demo": {
                        "host": "localhost",
                        "username": "tester",
                        "remote_root": "/srv/files",
                        "local_root": str(tmp_path),
                    }
                }
            }
        )
    )
    profile = load_profile("demo", config)
    assert profile.protocol == "sftp"
    assert profile.port == 22
    assert profile.remote_root == "/srv/files"
    assert profile.read_only is True


def test_plain_ftp_needs_explicit_permission(tmp_path):
    from portparcel.config import load_profile
    from portparcel.errors import TransferError

    config = tmp_path / "connections.json"
    config.write_text(
        json.dumps(
            {
                "connections": {
                    "demo": {
                        "host": "localhost",
                        "username": "tester",
                        "protocol": "ftp",
                        "remote_root": "/",
                        "local_root": str(tmp_path),
                    }
                }
            }
        )
    )
    with pytest.raises(TransferError, match="allow_insecure_ftp"):
        load_profile("demo", config)
