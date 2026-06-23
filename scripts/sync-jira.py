#!/usr/bin/env python3
"""
sync-jira.py — mirror the *authenticated* user's Jira issues (with full comment
threads) into a local folder.

Portable by design: the identity comes from the local ``acli`` auth — JQL uses
``currentUser()`` and the site/cloudId are whatever ``acli`` is logged into.
Nothing about any particular person, site, or project is hardcoded.

Usage:
  sync-jira.py [--out DIR] [--all] [--days N] [--exclude-projects A,B] [--subtasks]

  --out DIR             Output root (default: ./comms). Issues go to <out>/jira/.
  --all                 All involved issues (default: only open / not-Done).
  --days N              Also include issues updated within the last N days.
  --exclude-projects    Comma-separated project keys to skip (e.g. SEC for
                        automated dependency-scan noise). Default: none.
  --subtasks            Also dump each issue's sub-tickets (default: off).

"Involved" = assignee OR reporter OR watcher = currentUser().

Produces:
  <out>/jira/<KEY>/ticket.md (+ attachments/, comments, ...)
  <out>/jira/_keys.txt        # one issue key per line (input for the Slack step)

Requires: `acli` (authenticated) and the `inspecting-jira-issues` skill, whose
`jira-to-markdown.py` does the per-issue rendering. Install it with:
  npx skills add sunfmin/inspecting-jira-issues
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Statuses that are terminal but sometimes mis-categorised in Jira (their
# statusCategory isn't "Done"), so a category filter alone leaks them.
TERMINAL_STATUS_NAMES = {
    "done", "closed", "resolved", "cancelled", "canceled",
    "wont do", "won't do", "won’t do", "abandoned",
}


def find_jira_dump() -> Path | None:
    """Locate inspecting-jira-issues' jira-to-markdown.py (override: JIRA_TO_MARKDOWN)."""
    env = os.environ.get("JIRA_TO_MARKDOWN")
    candidates = [Path(env)] if env else []
    candidates += [
        Path.home() / ".claude/skills/inspecting-jira-issues/jira-to-markdown.py",
        Path.home() / ".agents/skills/inspecting-jira-issues/jira-to-markdown.py",
    ]
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            pass
    return None


def acli_site() -> str:
    """Best-effort Jira site URL from acli, for nicer browse links (optional)."""
    try:
        out = subprocess.run(["acli", "jira", "auth", "status"],
                             capture_output=True, text=True).stdout
    except OSError:
        return ""
    m = re.search(r"Site:\s*(\S+)", out)
    if not m:
        return ""
    site = m.group(1)
    return site if site.startswith("http") else f"https://{site}"


def involved_keys(scope_jql: str) -> list[str]:
    """Return issue keys for the current user, applying the terminal-name filter."""
    jql = (
        "(assignee = currentUser() OR reporter = currentUser() "
        "OR watcher = currentUser())" + scope_jql + " ORDER BY updated DESC"
    )
    proc = subprocess.run(
        ["acli", "jira", "workitem", "search", "--jql", jql,
         "--fields", "key,status", "--json", "--limit", "500"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"acli search failed:\n{proc.stderr or proc.stdout}")
    keys: list[str] = []
    for issue in json.loads(proc.stdout or "[]"):
        name = (((issue.get("fields") or {}).get("status") or {}).get("name") or "")
        if name.strip().lower() in TERMINAL_STATUS_NAMES:
            continue
        if issue.get("key"):
            keys.append(issue["key"])
    return keys


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--out", default="comms")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--exclude-projects", default="")
    ap.add_argument("--subtasks", action="store_true")
    args = ap.parse_args(argv[1:])

    dump = find_jira_dump()
    if dump is None:
        sys.exit("inspecting-jira-issues skill not found. Install it with:\n"
                 "  npx skills add sunfmin/inspecting-jira-issues\n"
                 "(or set JIRA_TO_MARKDOWN to its jira-to-markdown.py)")

    # Build the scope clause.
    clauses: list[str] = []
    if not args.all:
        clauses.append("statusCategory != Done")
    if args.days > 0:
        # OR-in recently updated so closed-but-fresh discussions are kept.
        if clauses:
            clauses[-1] = f"({clauses[-1]} OR updated >= -{args.days}d)"
        else:
            clauses.append(f"updated >= -{args.days}d")
    for proj in [p.strip() for p in args.exclude_projects.split(",") if p.strip()]:
        clauses.append(f"project != {proj}")
    scope = (" AND " + " AND ".join(clauses)) if clauses else ""

    keys = involved_keys(scope)

    out_root = Path(args.out)
    jira_dir = out_root / "jira"
    jira_dir.mkdir(parents=True, exist_ok=True)

    site = acli_site()
    env = dict(os.environ)
    if site:
        env["JIRA_SITE"] = site

    extra = [] if args.subtasks else ["--no-subtasks"]
    done = 0
    for key in keys:
        cmd = [sys.executable, str(dump), key, str(jira_dir / key), *extra]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if proc.returncode == 0:
            done += 1
            print(f"synced: {key}")
        else:
            tail = (proc.stderr or proc.stdout).strip().splitlines()
            print(f"FAILED {key}: {tail[-1] if tail else '?'}", file=sys.stderr)

    (jira_dir / "_keys.txt").write_text("\n".join(keys) + ("\n" if keys else ""))
    print(f"\nJira: {done}/{len(keys)} issue(s) → {jira_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
