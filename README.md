# mywork

Sync your work communications into a local folder of Markdown — **Jira** issues
(with full comment threads) and **Slack** (mentions, DMs, and threads that
discuss your Jira issues).

Portable by design: the identity is taken from whatever `acli` (Jira) and the
Slack connection are logged in as. Nothing about any person, site, or project is
hardcoded, so anyone can install and use it.

## Install

```bash
npx skills add sunfmin/mywork
```

## Prerequisites

- [`acli`](https://developer.atlassian.com/cloud/acli/) authenticated to your
  Jira site — `acli auth login`.
- The **inspecting-jira-issues** skill (renders each issue, with comments and
  attachments): `npx skills add sunfmin/inspecting-jira-issues`.
- **Slack** available as an MCP tool in your agent session (for the Slack half).

## Usage

This is an agent skill. In Claude Code, invoke it (e.g. `/mywork`) and it will:

1. Run the headless Jira sync:
   ```bash
   python3 scripts/sync-jira.py --out ./comms [--days 90] [--exclude-projects SEC] [--all]
   ```
2. Pull your Slack activity (mentions, DMs, threads you're in) and any threads
   mentioning your Jira keys, via the Slack MCP tools.

Output lands in `./comms/` (`jira/` + `slack/`). See `SKILL.md` for the full
procedure and the output layout.

## Jira scope flags

| Flag | Effect |
|------|--------|
| *(default)* | Open issues you're assignee / reporter / watcher of |
| `--days N` | Also include issues updated in the last N days (keeps fresh closed discussions) |
| `--all` | Every involved issue, regardless of status (can be large) |
| `--exclude-projects A,B` | Skip these project keys (e.g. `SEC` for automated dependency-scan tickets) |
| `--subtasks` | Also dump each issue's sub-tickets |
