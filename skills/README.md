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

### Claude Code

Skills in a project's `skills/` directory are picked up automatically
when Claude Code runs with that project as the working directory. No
config needed.

To install globally (available across all projects):

```bash
mkdir -p ~/.claude/skills/
cp -r skills/whoop-insights ~/.claude/skills/
```

### Cursor / Windsurf / Zed

Consult your editor's skill / agent-rule loading docs. The `SKILL.md`
file is the prompt; the supporting files live in the same directory.

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
