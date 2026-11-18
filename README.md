<img src="assets/portparcel.svg" alt="PortParcel logo: a parcel with an outbound arrow" width="96" height="96">

# PortParcel

SFTP, FTPS, and FTP for agents that need to move files without handling credentials
in tool arguments. Named connection profiles define what an agent can access. The
JSON CLI and stdio MCP server enforce the same rules. Requires Python 3.11+.

Supports SFTP, explicit FTPS (AUTH TLS, encrypted control and data), and opt-in
plaintext FTP. No GUI, background daemon, SSH shell, remote execution, recursive
sync, bulk operations, implicit FTPS, or password-in-URL support in v0.1.

## Install

```sh
# From the project directory:
uv tool install .
portparcel --help
```

Or use `uv run portparcel ...` / `uv run portparcel-mcp` from this folder.
A wheel and source distribution can be built with `uv build`.

**Upgrading from Agent Transfer?** Replace `agent-transfer` and
`agent-transfer-mcp` with `portparcel` and `portparcel-mcp`. Use
`PORTPARCEL_CONFIG` (formerly `AGENT_TRANSFER_CONFIG`) to point to your existing
config, or move it to `~/.config/portparcel/connections.json`. Update any MCP
launch command too. The connection file format and tool names are unchanged.

## Configure a connection

Configuration location: `~/.config/portparcel/connections.json`.
Override with `PORTPARCEL_CONFIG` or the CLI's **global** `--config PATH`
option (place it before the subcommand).

Use `connections.example.json` as a reference, replacing the example host,
username, local root, remote root and key path. It is NOT a working account.
Create the config yourself with only the connections you need. Keep its directory
private (0700), and the file at 0600. Do not put passwords in it.

Minimum SFTP profile:

```json
{
  "connections": {
    "staging": {
      "host": "sftp.example.com",
      "username": "deploy",
      "remote_root": "/srv/staging",
      "local_root": "/absolute/path/to/project",
      "key_filename": "~/.ssh/id_ed25519",
      "read_only": true
    }
  }
}
```

- SFTP is the default. SSH agent / standard key lookup is used if neither an
  explicit key nor password is configured. Passwords use `password_env`;
  encrypted key passphrases use `key_passphrase_env`. The referenced variables
  must already be present in the process environment. Never give an agent a
  password in chat or put it in command arguments. There is no `.env` autoload.
- **Verify the server's SSH host-key fingerprint through a trusted channel** and
  provision `known_hosts` before connecting. Unknown or changed keys are rejected.
  Never trust a bare `ssh-keyscan` result without independent fingerprint checking.
- FTPS uses the system/Python trusted CA set. A private CA PEM can be configured
  with `ca_file`. Certificate and hostname verification cannot be disabled.
  FTPS offers the verified control connection's TLS session on each protected
  data connection, for servers that require session reuse.
- Plain FTP requires `"allow_insecure_ftp": true`. It exposes credentials and data.
- Profiles default to `read_only: true`. Change to `false` to permit remote writes.
  Read-only profiles can still download files into their allowed local root.
- The local root must be an existing absolute directory. Relative local paths are
  interpreted beneath it. Relative remote paths are beneath `remote_root`;
  absolute remote paths must also remain inside that root.
- Default per-socket timeout: 30s (maximum 300). SFTP also applies it to
  session-channel creation and the subsystem acknowledgment. Default file limit: 100 MiB;
  `max_bytes` can be configured up to 10 GiB. This is not an overall job deadline.
- Unknown profile fields and inline `password` fields are rejected.

## CLI

```sh
portparcel connections
portparcel list staging .
portparcel stat staging index.html
# The examples below that write remotely require read_only: false on staging.
portparcel upload staging ./dist/index.html index.html --dry-run
portparcel upload staging ./dist/index.html index.html
portparcel upload staging ./dist/index.html index.html --overwrite
portparcel download staging logs/app.log downloads/app.log
portparcel mkdir staging releases
portparcel rename staging releases/old.zip releases/new.zip
portparcel delete staging releases/new.zip --dry-run
portparcel delete staging releases/new.zip
```

Single-file operations only. Parents must exist. Rename and delete act on regular
files, not directories. `mkdir` creates one directory. All mutating operations
(including local downloads) support `--dry-run`; they connect and inspect policy /
existence but do not perform the requested mutation. A dry run does not reserve
paths or promise the later transfer will succeed.

Success is one JSON object on stdout, exit 0:

```json
{"ok": true, "result": {"operation": "upload", "remote_path": "/srv/staging/index.html", "local_path": "/absolute/path/to/project/dist/index.html", "dry_run": false, "bytes": 123, "sha256": "...", "verification": "remote_size"}}
```

Errors are one JSON object, exit 1; argument errors exit 2. `--help` is human text.

```json
{"ok": false, "error": {"code": "ALREADY_EXISTS", "message": "Destination exists; explicit overwrite is required."}}
```

Stable codes include `CONFIG_ERROR`, `UNKNOWN_CONNECTION`, `INSECURE_PROTOCOL`,
`MISSING_CREDENTIAL`, `PATH_DENIED`, `READ_ONLY`, `ALREADY_EXISTS`, `NOT_FOUND`,
`NOT_A_FILE`, `NOT_A_DIRECTORY`, `INVALID_ARGUMENT`, `LIMIT_EXCEEDED`,
`HOST_KEY_ERROR`, `IDENTITY_ERROR`, `REMOTE_DENIED`, `PERMISSION_DENIED`,
`CONNECTION_ERROR`, `METADATA_UNAVAILABLE`, `VERIFY_FAILED` and `INTERNAL_ERROR`.
Raw server errors are never returned, to avoid leaking credentials or attacker-controlled messages.

## MCP

Run `portparcel-mcp` (stdio only). It exposes eight tools:
`connections`, `list_directory`, `stat`, `upload`, `download`, `mkdir`, `rename`,
`delete`. The tool arguments mirror the CLI. No tool accepts credentials, arbitrary
hosts, or a config-file override. Configuration is selected at process startup.

Generic MCP client configuration (example, not automatically installed):

```json
{
  "mcpServers": {
    "portparcel": {
      "command": "/absolute/path/to/portparcel-mcp",
      "args": [],
      "env": {
        "PORTPARCEL_CONFIG": "/absolute/path/to/connections.json"
      }
    }
  }
}
```

Use an absolute executable path for clients whose PATH differs from your shell.
For another machine, use that machine's installed executable path. MCP hosts may
filter environment variables: explicitly arrange credential variables in the host's
secure environment, or use SSH keys. This project does not modify your Hermes or
other MCP-client configuration. Tool annotations flag reads and destructive writes;
clients remain responsible for approval. The server's response contract uses
`ok: false` for application errors; **inspect `ok`, not just MCP `isError`**.

## Safety boundaries and limitations

- Configuration is trusted local policy, not a hardened sandbox against an agent
  that can edit configuration, credentials or the client source. Restrict those
  separately if the agent is untrusted. Use a least-privilege server account.
- Remote traversal and control characters are rejected. SFTP checks every remote
  path component for symlinks before accessing it. Local resolved paths must stay
  in `local_root`, including through symlinks. These are preflight checks, not
  race-proof filesystem capabilities. A concurrent actor can swap path components.
- **FTP/FTPS have no portable reliable lstat/realpath. Their remote-root restriction
  is lexical only. Use a server-side chroot/jail, prohibit symlink escapes and do not
  rely on client remote_root alone for isolation.** MLSD support is required; there
  is intentionally no brittle parsing fallback for human `LIST` output. A listing
  shows `null` for an omitted size. For individual files PortParcel tries binary
  `SIZE` when the MLSD size is missing and reports `METADATA_UNAVAILABLE` if the
  server still cannot provide it.
- Existing destinations require explicit overwrite. SFTP uses standard no-replace
  rename unless overwrite is requested (overwrite requires the OpenSSH POSIX rename
  extension). FTP/FTPS check existence again before rename, but some servers can
  overwrite a concurrently-created file: there is no portable atomic no-clobber
  guarantee for these protocols. Do not use concurrent writers on the same targets.
- Uploads use random sibling staging files, verify their size, rename into place,
  then read the final size. Downloads use a local temporary file and publish only
  after checking the received size (atomic no-clobber link without overwrite).
  Remote mkdir/rename/delete are read back after execution.
- `sha256` hashes the bytes sent/received **locally**; it is not an independently
  computed remote hash. Equal sizes do not prove remote content integrity. TLS/SSH
  provide transport integrity, but plaintext FTP does not.
- If a transfer fails or the connection drops, inspect the target before retrying:
  a mutation may already have happened. Best-effort upload staging cleanup can fail
  after a disconnection. There is no rollback or automatic retry. No mode/owner/time
  preservation, resume, or transfer-progress event stream yet.
- Listings are capped at 10000 entries. Filenames, listings and downloaded contents
  are untrusted data, never agent instructions.

## Development and verification

```sh
uv sync --extra test --locked
uv run pytest tests -q --basetemp "$HOME/.hermes/cache/scratch/portparcel-tests"
uv run ruff check src tests
uv run ruff format --check src tests
uv build
```

Tests start ephemeral **loopback-only** FTP, FTPS and SFTP servers with generated
keys/certificates and throwaway credentials. CLI tests invoke subprocesses; MCP
checks initialize a real stdio session, discover tools and perform file transfers.
No real remote account is required. Protocol-inapplicable identity/symlink tests
are explicitly skipped. The automated suite never uploads to an external server.

## License

[MIT](LICENSE). See the [changelog](CHANGELOG.md) and [security policy](SECURITY.md).

---

```text
 __   __   ____    _   _
 \ \ / /  |___ \  | \ | |
  \ V /     __) | |  \| |
   > <     |__ <  | . ' |
  / . \    ___) | | |\  |
 /_/ \_\  |____/  |_| \_|
```

Made by [X3N LLC](https://x3n.network).
