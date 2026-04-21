# Security Policy

## Supported versions

Security patches land on the latest released `0.x` line. See
[CHANGELOG.md](./CHANGELOG.md) for the current version.

## Reporting a vulnerability

Please **do not** file public GitHub issues for security reports.

Email: **k.l.ashwanthram@gmail.com**

Include: a description, reproduction steps, impact, and (optionally) a
suggested fix. You can expect acknowledgement within 48 hours and a
status update within a week.

## What the server does with sensitive data

- Tokens (access + refresh) are encrypted at rest under a machine-local
  Fernet key in `~/.whoop-mcp-server/`. Both files are created with
  file mode `0o600`.
- Fitness data is cached in a local SQLite database at
  `~/.whoop-mcp-server/whoop.db`, also `0o600`.
- Log files rotate in `~/.whoop-mcp-server/logs/` and never contain
  tokens, encryption keys, or raw API response bodies.
- The server makes outbound requests only to `api.prod.whoop.com` —
  no third-party services, no telemetry, no cloud sync.

See [PRIVACY.md](./PRIVACY.md) for the full data-handling statement.

## Threat model

Designed for single-user, personal, trusted-device use. Not designed
for multi-user servers, shared accounts, or untrusted networks.
