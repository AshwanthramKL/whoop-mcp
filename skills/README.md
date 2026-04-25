# Skills

Claude Code / Cursor / Windsurf-compatible skills that use the `whoop`
MCP server. A skill is a prompt bundle (markdown + optional supporting
files) the agent picks up and runs when the user's request matches its
trigger description.

## Installed skills

| Skill | Trigger phrases | What it does |
|-------|-----------------|--------------|
| [whoop-insights](./whoop-insights/SKILL.md) | "how am I doing", "whoop weekly report", "am I overtraining", "show me my whoop dashboard", "find patterns in my whoop" | Pulls 30 days of data, computes personal baselines, flags anomalies, runs two specific correlations (sleep→recovery, strain→recovery), outputs a written report; optionally generates a self-contained HTML dashboard. |

## Installing a skill

### Recommended (one command, all install paths)

```bash
whoop-mcp-install-skills
```

Writes to `~/.claude/skills/` by default. Works identically whether
you installed via `uvx`, `pipx`, `pip`, or a git clone — the script
fetches the latest skill from this repo's `main` branch over HTTPS.

Override the target with `--target <dir>` (e.g. for project-scoped
skills). Pass `--source <path-to-local-skills-dir>` to install from a
local working tree instead of GitHub — useful when you're editing a
skill and want to test before pushing.

### Manual fallback

If you can't run the script (no network, locked-down environment, etc.):

```bash
mkdir -p ~/.claude/skills/
cp -r skills/whoop-insights ~/.claude/skills/
```

### Cursor / Windsurf / Zed

The `SKILL.md` file is just a markdown prompt — these clients pick it
up from the same `~/.claude/skills/` path Claude Code uses on macOS,
or their own equivalent. Consult your editor's skill / agent-rule
loading docs.

## Contributing a skill

1. Create `skills/<your-skill-name>/SKILL.md` with frontmatter:
   ```yaml
   ---
   name: your-skill-name
   description: When to trigger (the agent matches against this).
   ---
   ```
2. Add supporting files (templates, example data) in the same directory.
3. Update the table in this README.
4. Open a PR.

Keep skills **specific**. "Analyze my whoop" is too vague. "Generate a
weekly overtraining-risk report with anomaly flags" is the right size.
