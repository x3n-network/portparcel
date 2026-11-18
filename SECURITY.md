# Security policy

Report a suspected vulnerability privately through this repository’s **Security → Report a vulnerability** form. If the form is unavailable, contact X3N LLC via [x3n.network](https://x3n.network) to arrange private disclosure. Do not post vulnerability details, credentials, connection profiles, or server listings in a public issue.

Include the PortParcel version, protocol, expected and observed behavior, and a minimal example using dummy data or a local test server. Redact hostnames, usernames, and paths if they are sensitive.

PortParcel is not a sandbox. Use a dedicated least-privilege account, keep profiles read-only unless remote writes are needed, and keep credentials outside agent prompts. For FTP/FTPS, the server must enforce directory isolation; the client’s remote-root check is lexical. Prefer SFTP or FTPS over plaintext FTP. See [the README](README.md#safety-boundaries-and-limitations) for the full limitations.
