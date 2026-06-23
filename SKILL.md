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

Then do the **Slack step**. There are two paths. Just run `sync-slack.py` — it
self-detects [`slackdump`](https://github.com/rusq/slackdump) and signals via its
exit code which path applies:

```bash
python3 ~/.claude/skills/mywork/scripts/sync-slack.py --comms ./comms   # --days 14
#   exit 0  -> done, headless (Path 1)
#   exit 2  -> slackdump not installed     } do Path 2
#   exit 3  -> slackdump not authenticated } (agent fetch, below)
```

Both paths converge on the **same** `render-slack.py`, so the Markdown output is
identical in shape. The raw `slack_read_thread` / `slackdump dump` output is the
single source of truth in `_raw/`; the `.md` files are derived (idempotent).

## Path 1 — slackdump (headless)

If `slackdump` is installed and a workspace is authenticated (one-time
`slackdump workspace new`), `sync-slack.py` does everything with no agent:
searches `from:me` / `to:me` (bounded by `--days`) + each Jira key, dumps each
matching thread, converts to `comms/slack/_raw/*.txt`, and runs `render-slack.py`.
It's cron-able. Notes:

- It owns the slack output: each run wipes `_raw/*.txt` + `involved|by-jira/*.md`
  and regenerates. Exit `2` = slackdump absent, `3` = not authenticated.
- More complete than Path 2 (slackdump paginates the full window; the MCP search
  caps at ~20 hits/query). Standalone (non-thread) chat fragments are skipped for
  *involved*; a standalone by-jira hit is synthesized from the search payload.

## Path 2 — agent fetches → script renders (fallback)

Use when `slackdump` is not installed. *Fetching* needs the in-session Slack MCP
tools (only an MCP client can call the claude.ai connector), so it's agent-driven
— not cron-able. The agent dumps each `slack_read_thread` result **verbatim** to
a raw file; `render-slack.py` then formats it (no model tokens spent on layout).

Default window: **last 14 days** (compute the unix cutoff, pass as `after=`).

### 1. Fetch + dump raw (agent)

**A. What I'm involved in** (`category: involved`)
1. Mentions + DMs to me:
   `slack_search_public_and_private(query="to:me", sort="timestamp", after="<cutoff>")`
2. Threads I've spoken in:
   `slack_search_public_and_private(query="from:me", sort="timestamp", after="<cutoff>")`
3. De-dupe by `(channel_id, thread_ts)`; skip bot-only DMs (Jira, Kolide,
   Google Calendar/Drive, Spock, …).

**B. Threads about my Jira issues** (`category: by-jira`)
1. Read keys from `comms/jira/_keys.txt`.
2. For each `KEY`: `slack_search_public_and_private(query="KEY")` (keyword).
   Skip keys whose only hits are the Jira bot's assignment nudges.

For **every** thread kept, call `slack_read_thread` and write its **verbatim**
output to `comms/slack/_raw/<name>.txt`, preceded by an `@@meta` header you fill
from the *search* result (the thread body alone lacks channel name / permalink):

```
@@meta
slug: dm-xuxin-20260623-1782201646     # output basename; involved only
kind: dm                               # dm | channel
peer: Xu Xin                           # the other DM participant (dm only)
channel_name: ai-coe                   # channel name w/o '#' (channel only)
channel_id: D82B0SG9M
thread_ts: 1782201646.318239
permalink: https://theplant.slack.com/archives/...
category: involved                     # involved | by-jira
owner: felix                           # the authenticated user (marks "(me)")
jira_key: ISMS-610                     # by-jira only (file is named <jira_key>.md)
jira_summary: [RISK25] ...             # by-jira only
@@body
<paste the slack_read_thread output here, unchanged>
```

Display names need **no** `slack_read_user_profile` lookup — they're already in
each `From:` line of the thread output, and the renderer parses them.

### 2. Render Markdown (script)

```bash
python3 ~/.claude/skills/mywork/scripts/render-slack.py --comms ./comms \
    --owner "<your display name>"   # marks your messages "(me)"; --tz +08:00 to override
```

Parses every `_raw/*.txt` and writes `involved/<slug>.md` + `by-jira/<KEY>.md`
**plus `index.md` + `_index.json`** (a triage manifest). It is tuned for an AI
reader: YAML front-matter per file, full timestamps under a declared `tz`, an
`(me)` marker for the owner, Jira keys cross-linked, and normalized noise (emoji
skin-tones stripped, Tencent-invite boilerplate collapsed, attachments typed as
`[image: …]` / `[file: …]`). Resolves Slack markup (`<@U|name>`→`@name`,
`<!here>`, `<url|text>`→links, HTML entities). Deterministic and idempotent.

### Markdown shape it produces

```markdown
---
kind: dm | channel
channel: "#ai-coe" | "DM with <peer>"
participants: ["<name>", ...]
owner: <name>                 # whoever is "(me)"
category: involved | by-jira
date_start: 2026-06-17T17:36+08:00
date_end:   2026-06-22T19:41+08:00
tz: "+08:00"
messages: 7
jira: [ISMS-610]              # keys mentioned in the thread
permalink: https://...
---
# #<channel> — <gist>

<permalink>

**<Display Name> (me)** · <YYYY-MM-DD HH:MM>
<message text>
_(reactions: ok_hand (1) · [image: shot.png])_

  ↳ **<Replier>** · <YYYY-MM-DD HH:MM>
  <reply text>
```

## Output layout

```
comms/
├── jira/
│   ├── _keys.txt                 # issue keys (input for Slack step B)
│   └── <KEY>/ticket.md           # full dump incl. comments (+ attachments/)
└── slack/
    ├── _raw/<name>.txt              # verbatim slack_read_thread + @@meta header
    ├── index.md                     # triage manifest (newest-first table)
    ├── _index.json                  # machine-readable sibling of index.md
    ├── involved/<slug>.md           # generated by render-slack.py
    └── by-jira/<KEY>.md             # generated by render-slack.py
```

Re-running is safe and idempotent: the Jira step overwrites each issue folder;
for Slack, overwrite the `_raw/*.txt` dumps and re-run `render-slack.py` (the
`.md` files are derived — never hand-edit them, edit the raw dump or the script).

## Prerequisites

- **acli**, authenticated to your Jira site (`acli auth login`).
- **inspecting-jira-issues** skill (does the per-issue Markdown rendering):
  `npx skills add sunfmin/inspecting-jira-issues`
- **Slack**, either path:
  - *Path 1* — [`slackdump`](https://github.com/rusq/slackdump) installed
    (`brew install slackdump`) + authenticated (`slackdump workspace new`).
    Fully headless / cron-able.
  - *Path 2* — Slack connected as an MCP tool in the session (agent-driven).

The Jira script auto-locates `jira-to-markdown.py` under `~/.claude/skills/` or
`~/.agents/skills/` (override with the `JIRA_TO_MARKDOWN` env var).
