import asyncio
import datetime
import json
import threading

import asyncssh
import pytest
from OpenSSL import SSL
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler, TLS_DTPHandler, TLS_FTPHandler
from pyftpdlib.ioloop import IOLoop
from pyftpdlib.servers import FTPServer


@pytest.fixture(params=["ftp", "ftps", "sftp"])
def endpoint(request, tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    local = tmp_path / "local"
    remote.mkdir()
    local.mkdir()
    tls_version = {"ftps-reuse-12": SSL.TLS1_2_VERSION, "ftps-reuse-13": SSL.TLS1_3_VERSION}.get(
        request.param
    )
    protocol = "ftps" if tls_version else request.param
    monkeypatch.setenv("PORTPARCEL_TEST_PASSWORD", "loopback-test-only")
    profile = {
        "protocol": protocol,
        "host": "localhost",
        "username": "tester",
        "password_env": "PORTPARCEL_TEST_PASSWORD",
        "remote_root": "/",
        "local_root": str(local),
        "read_only": False,
    }
    if protocol in ("ftp", "ftps"):
        authorizer = DummyAuthorizer()
        authorizer.add_user("tester", "loopback-test-only", str(remote), perm="elradfmwMT")
        base = TLS_FTPHandler if protocol == "ftps" else FTPHandler

        class Handler(base):
            def ftp_MLSD(self, path):
                if request.node.name.startswith(
                    "test_list_configured_root_without_parent_listing"
                ) and path == str(remote):
                    self.respond("550 Parent listing denied.")
                    return None
                return super().ftp_MLSD(path)

        Handler.authorizer = authorizer
        if protocol == "ftps":
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            now = datetime.datetime.now(datetime.timezone.utc)
            cert = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
                .sign(key, hashes.SHA256())
            )
            cert_path = tmp_path / "cert.pem"
            key_path = tmp_path / "key.pem"
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            Handler.certfile = str(cert_path)
            Handler.keyfile = str(key_path)
            Handler.tls_control_required = True
            Handler.tls_data_required = True
            if tls_version:
                context = Handler.get_ssl_context()
                context.set_min_proto_version(tls_version)
                context.set_max_proto_version(tls_version)
                context.set_session_id(b"portparcel-ftps-test")
                context.set_session_cache_mode(SSL.SESS_CACHE_SERVER)

                class ReuseRequired(TLS_DTPHandler):
                    def handle_ssl_established(self):
                        # pyOpenSSL does not expose session_reused on its public Connection API.
                        if not SSL._lib.SSL_session_reused(self.socket._ssl):
                            self.cmd_channel.respond("425 TLS session resumption required.")
                            self.close()

                Handler.dtp_handler = ReuseRequired
            profile["ca_file"] = str(cert_path)
        else:
            profile["allow_insecure_ftp"] = True
        loop = IOLoop()
        server = FTPServer(("127.0.0.1", 0), Handler, ioloop=loop)
        profile["port"] = server.socket.getsockname()[1]
        stopping = threading.Event()

        def serve():
            while not stopping.is_set():
                loop.loop(timeout=0.01, blocking=False)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        def stop():
            stopping.set()
            thread.join(3)
            server.close_all()
            loop.close()
    else:
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        key = asyncssh.generate_private_key("ssh-ed25519")

        class Server(asyncssh.SSHServer):
            def begin_auth(self, username):
                return True

            def password_auth_supported(self):
                return True

            def validate_password(self, username, password):
                return username == "tester" and password == "loopback-test-only"

        async def start():
            return await asyncssh.create_server(
                Server,
                "127.0.0.1",
                0,
                server_host_keys=[key],
                sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(remote)),
            )

        server = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
        port = server.get_port()
        profile["port"] = port
        known_hosts = tmp_path / "known_hosts"
        known_hosts.write_text(f"[localhost]:{port} " + key.export_public_key().decode())
        profile["known_hosts"] = str(known_hosts)

        def stop():
            async def close():
                server.close()
                await server.wait_closed()

            asyncio.run_coroutine_threadsafe(close(), loop).result(10)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(3)
            loop.close()

    config = tmp_path / "connections.json"
    config.write_text(json.dumps({"connections": {"test": profile}}))
    yield config, local, remote, profile
    stop()


@pytest.mark.parametrize(
    "endpoint", ["ftp", "ftps", "sftp", "ftps-reuse-12", "ftps-reuse-13"], indirect=True
)
def test_transfer_lifecycle(endpoint):
    from portparcel.service import execute

    config, local, remote, _ = endpoint
    payload = b"Agent transfer\x00\xff\n" * 1000
    (local / "source.bin").write_bytes(payload)

    def call(operation, **kwargs):
        response = execute(operation, "test", config=config, **kwargs)
        assert response["ok"], response
        return response["result"]

    call("mkdir", remote_path="/files")
    call("upload", local_path="source.bin", remote_path="/files/data.bin", dry_run=True)
    assert not (remote / "files/data.bin").exists()
    result = call("upload", local_path="source.bin", remote_path="/files/data.bin")
    assert result["bytes"] == len(payload)
    assert (remote / "files/data.bin").read_bytes() == payload
    assert call("stat", remote_path="/files/data.bin")["size"] == len(payload)
    assert "data.bin" in [e["name"] for e in call("list", remote_path="/files")["entries"]]
    error = execute(
        "upload", "test", config=config, local_path="source.bin", remote_path="/files/data.bin"
    )
    assert error["error"]["code"] == "ALREADY_EXISTS"
    call("download", remote_path="/files/data.bin", local_path="download.bin")
    assert (local / "download.bin").read_bytes() == payload
    call("rename", remote_path="/files/data.bin", destination="/files/renamed.bin")
    assert not (remote / "files/data.bin").exists()
    assert (remote / "files/renamed.bin").exists()
    call("delete", remote_path="/files/renamed.bin", dry_run=True)
    assert (remote / "files/renamed.bin").exists()
    call("delete", remote_path="/files/renamed.bin")
    assert not (remote / "files/renamed.bin").exists()


@pytest.mark.parametrize("endpoint", ["ftp", "ftps"], indirect=True)
def test_list_configured_root_without_parent_listing(endpoint):
    from portparcel.service import execute

    config, _, remote, profile = endpoint
    (remote / "allowed").mkdir()
    (remote / "allowed" / "file.txt").write_text("file")
    assert (
        execute("list", "test", config=config, remote_path="/")["error"]["code"] == "REMOTE_DENIED"
    )
    profile["remote_root"] = "/allowed"
    config.write_text(json.dumps({"connections": {"test": profile}}))
    listing = execute("list", "test", config=config, remote_path=".")
    assert listing["ok"], listing
    assert [item["name"] for item in listing["result"]["entries"]] == ["file.txt"]
    assert execute("stat", "test", config=config, remote_path=".")["result"]["type"] == "directory"


def test_policy_refusals(endpoint):
    from portparcel.service import execute

    config, local, remote, profile = endpoint
    (local / "a").write_text("abc")
    (remote / "a").write_text("abc")
    for operation, args in [
        ("stat", {"remote_path": "/../escape"}),
        ("download", {"remote_path": "/a", "local_path": "../escape"}),
        ("stat", {"remote_path": "/a\r\nDELE a"}),
    ]:
        result = execute(operation, "test", config=config, **args)
        assert result["error"]["code"] == "PATH_DENIED", result
    profile["read_only"] = True
    config.write_text(json.dumps({"connections": {"test": profile}}))
    result = execute("delete", "test", config=config, remote_path="/a")
    assert result["error"]["code"] == "READ_ONLY"
    assert (remote / "a").exists()
