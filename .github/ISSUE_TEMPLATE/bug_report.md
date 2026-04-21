---
name: Bug report
about: Something in whoop-mcp is broken or behaves unexpectedly
title: "[bug] "
labels: bug
---

### What happened

<!-- A short, factual description. "health_check returned unhealthy" / "export_whoop crashed" / "Claude got the wrong bedtime". Skip adjectives. -->

### What you expected

<!-- One sentence. -->

### Repro

<!-- Minimal steps. Ideally a single tool call with arguments, or a single CLI invocation. -->

1.
2.
3.

### Environment

- `whoop-mcp` version: <!-- run `whoop-mcp` → server logs version, OR `pip show whoop-mcp | grep Version` -->
- Python version: <!-- python --version -->
- OS: <!-- macOS 15 / Ubuntu 22.04 / Windows 11 -->
- MCP client: <!-- Claude Code 2.x / Claude Desktop / Cursor / etc. -->
- Install method: <!-- uvx / pipx / pip / git clone -->

### `health_check` output

<!-- Run `health_check()` from your MCP client and paste the result here. If you can't get health_check to run at all, say so. -->

```json
```

### Logs

<!-- Relevant lines from ~/.whoop-mcp-server/logs/whoop-mcp.log. Redact anything that looks like a token (Bearer, JWT, base64 blob) before pasting. -->

```
```

### Anything else

<!-- Links to related issues, speculation about root cause, things you already tried. Optional. -->
