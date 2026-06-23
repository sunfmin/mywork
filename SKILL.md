---
name: mywork
description: Sync the authenticated user's work communications into a local folder of Markdown — Jira issues (with full comment threads) and Slack (mentions, DMs, and threads discussing your Jira issues). Identity is taken from whatever acli (Jira) and the Slack connection are logged in as — nothing is hardcoded, so anyone can install and use it. Use when the user wants to mirror / sync / archive their Slack + Jira activity locally, keep a local copy of work conversations, or says "sync my work" / "sync my comms".
---

# mywork — sync my work communications to local Markdown

Mirrors the **currently authenticated** person's work comms into one folder:

- **Jira** — every issue you're involved in (assignee / reporter / watcher),
  full dump including the **comment thread** (the actual discussion).
- **Slack** — (A) what you're involved in (mentions, DMs, threads you're in)
  and (B) threads that mention your Jira issue keys, cross-linking the two.

## Identity comes from auth — never hardcode it

This skill is meant to be installed by anyone. Do **not** put a name, email,
Slack user id, Jira site, or project key in any command:

- **Jira**: JQL uses `currentUser()`; the site/cloudId come from `acli`'s login.
- **Slack**: use the `to:me` / `from:me` search modifiers, which resolve to the
  logged-in Slack user. (The Slack MCP also reports the logged-in `user_id` in
  its tool descriptions — read it at runtime; don't bake it into files.)

## Quick start

```bash
# 1. Jira (headless). Default = open issues you're involved in.
python3 ~/.claude/skills/mywork/scripts/sync-jira.py --out ./comms
#   add --days 90 to include recently-touched closed issues
#   add --exclude-projects SEC to skip automated dependency-scan tickets
#   add --all for every involved issue (can be large)
```

Then do the **Slack step** below (it needs the in-session Slack MCP tools, so
it's agent-driven, not a cron script).

## Slack step (agent procedure)

Work into `./comms/slack/`. Default window: **last 14 days** (compute the unix
cutoff and pass it as `after=`). For each thread you find, call
`slack_read_thread` to capture the full conversation, then write Markdown.

**A. What I'm involved in → `comms/slack/involved/`**

1. Mentions + DMs addressed to me:
   `slack_search_public_and_private(query="to:me", sort="timestamp", after="<cutoff>")`
2. Threads I've spoken in:
   `slack_search_public_and_private(query="from:me", sort="timestamp", after="<cutoff>")`
3. De-dupe by `(channel_id, thread_ts)`; read each thread; write one file per
   thread: `involved/<channel-name>-<YYYYMMDD>-<ts>.md`.

**B. Threads about my Jira issues → `comms/slack/by-jira/`**

1. Read the keys from `comms/jira/_keys.txt`.
2. For each `KEY`: `slack_search_public_and_private(query="KEY")` (keyword).
3. Read each matching thread; write `by-jira/<KEY>.md` (skip keys with no hits).
   Link back to the issue: `../jira/<KEY>/ticket.md`.

### Markdown shape for a thread

```markdown
# #<channel> — <parent message one-liner>

<permalink>

**<Display Name>** · <YYYY-MM-DD HH:MM>
<message text>

  ↳ **<Replier>** · <time>
  <reply text>
```

Resolve display names with `slack_read_user_profile` (cache per id); fall back
to the raw id if lookup fails.

## Output layout

```
comms/
├── jira/
│   ├── _keys.txt                 # issue keys (input for Slack step B)
│   └── <KEY>/ticket.md           # full dump incl. comments (+ attachments/)
└── slack/
    ├── involved/<channel>-<date>-<ts>.md
    └── by-jira/<KEY>.md
```

Re-running is safe: the Jira step overwrites each issue folder; for Slack,
overwrite the per-thread / per-key files.

## Prerequisites

- **acli**, authenticated to your Jira site (`acli auth login`).
- **inspecting-jira-issues** skill (does the per-issue Markdown rendering):
  `npx skills add sunfmin/inspecting-jira-issues`
- **Slack** connected as an MCP tool in the session (for the Slack step).

The Jira script auto-locates `jira-to-markdown.py` under `~/.claude/skills/` or
`~/.agents/skills/` (override with the `JIRA_TO_MARKDOWN` env var).
