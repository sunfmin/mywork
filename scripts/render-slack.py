#!/usr/bin/env python3
"""Render Slack thread Markdown from raw MCP output.

The Slack step of mywork is *agent-driven* for fetching — only an in-session
MCP client can call the claude.ai Slack connector. But formatting should not be:
the agent dumps each ``slack_read_thread`` result *verbatim* into a raw file,
and this script deterministically turns those into the final Markdown. That
keeps the raw MCP output as the single source of truth and makes re-rendering
idempotent (and free of model token cost / drift).

Raw file format (``comms/slack/_raw/<name>.txt``)::

    @@meta
    slug: dm-xuxin-20260623-1782201646     # output basename (involved only)
    kind: dm                               # dm | channel
    peer: Xu Xin                           # other DM participant (dm only)
    channel_name: ai-coe                   # channel name w/o # (channel only)
    channel_id: D82B0SG9M
    thread_ts: 1782201646.318239
    permalink: https://theplant.slack.com/archives/...
    category: involved                     # involved | by-jira
    jira_key: ISMS-610                     # by-jira only
    jira_summary: [RISK25] ...             # by-jira only
    @@body
    === THREAD PARENT MESSAGE ===
    From: Xu Xin <xuxin@theplant.jp> (U7JR6U8UD)
    Time: 2026-06-23 16:00:46 CST
    Message TS: 1782201646.318239
    ai pair 现在来吗
    ... (verbatim slack_read_thread output) ...

Output:
    involved/<slug>.md
    by-jira/<jira_key>.md

Usage:
    render-slack.py [--comms ./comms]
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

REPLY_SEP = re.compile(r"^--- Reply \d+ of \d+ ---\s*$")
PARENT_MARK = "=== THREAD PARENT MESSAGE ==="
REPLIES_MARK = re.compile(r"^=== THREAD REPLIES \((\d+) total\) ===\s*$")
FROM_RE = re.compile(r"^From:\s*(?P<name>.*?)(?:\s*<[^>]*>)?\s*\((?P<uid>[^)]*)\)\s*(?:\[BOT\])?\s*$")
TIME_RE = re.compile(r"^Time:\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}):\d{2}")
TS_RE = re.compile(r"^Message TS:\s*(\S+)")
REACT_RE = re.compile(r"^Reactions:\s*(.+)$")
FILES_RE = re.compile(r"^Files:\s*(.+)$")
NOISE = ("No thread messsages", "No thread messages", "There are no more messages")


def clean_text(s: str) -> str:
    """Resolve Slack markup: <@U|name>, <!here>, <url|text>, entities."""
    def angle(m: re.Match) -> str:
        inner = m.group(1)
        if inner.startswith("@"):                     # user mention
            body = inner[1:]
            return "@" + (body.split("|", 1)[1] if "|" in body else body)
        if inner.startswith("!"):                     # broadcast / subteam
            body = inner[1:]
            if "|" in body:
                return "@" + body.split("|", 1)[1]
            if body.startswith("subteam^"):
                return "@subteam"
            return "@" + body
        if "|" in inner:                              # <url|text>
            url, text = inner.split("|", 1)
            if url.startswith(("tel:", "mailto:")):
                return text
            return f"[{text}]({url})"
        return inner                                  # bare <url>
    return html.unescape(re.sub(r"<([^>]+)>", angle, s))


def parse_files(raw: str) -> list[str]:
    """'a.png (ID: F1, image/png, 1KB), b.xlsx (ID: F2, ...)' -> ['a.png','b.xlsx']."""
    return [f.strip() for f in re.findall(r"([^,(]+?)\s*\(ID:", raw) if f.strip()]


def parse_block(lines: list[str]) -> dict | None:
    """Parse one message block into {name, date, time, text, reactions, files}."""
    name = date = time = None
    text_lines: list[str] = []
    reactions: list[str] = []
    files: list[str] = []
    for ln in lines:
        if name is None and (m := FROM_RE.match(ln)):
            name = m.group("name").strip()
            continue
        if (m := TIME_RE.match(ln)):
            date, time = m.group(1), m.group(2)
            continue
        if TS_RE.match(ln):
            continue
        if (m := REACT_RE.match(ln)):
            reactions.append(m.group(1).strip())
            continue
        if (m := FILES_RE.match(ln)):
            files.extend(parse_files(m.group(1)))
            continue
        if any(n in ln for n in NOISE):
            continue
        text_lines.append(ln)
    if name is None:
        return None
    text = "\n".join(text_lines).strip("\n")
    return {"name": name, "date": date, "time": time,
            "text": clean_text(text).rstrip(),
            "reactions": reactions, "files": files}


def parse_body(body: str) -> list[dict]:
    """Split verbatim slack_read_thread output into ordered message blocks."""
    lines = body.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == PARENT_MARK)
    except StopIteration:
        return []
    lines = lines[start + 1:]
    # Split off the replies section, if any.
    rep_idx = next((i for i, l in enumerate(lines) if REPLIES_MARK.match(l)), None)
    parent_lines = lines[:rep_idx] if rep_idx is not None else lines
    blocks = [parse_block(parent_lines)]
    if rep_idx is not None:
        cur: list[str] = []
        for ln in lines[rep_idx + 1:]:
            if REPLY_SEP.match(ln):
                if cur:
                    blocks.append(parse_block(cur))
                cur = []
            else:
                cur.append(ln)
        if cur:
            blocks.append(parse_block(cur))
    return [b for b in blocks if b]


def parse_raw(path: Path) -> dict:
    txt = path.read_text(encoding="utf-8")
    meta_part, _, body = txt.partition("@@body")
    meta: dict[str, str] = {}
    for ln in meta_part.splitlines():
        if ln.strip() in ("", "@@meta"):
            continue
        k, _, v = ln.partition(":")
        meta[k.strip()] = v.strip()
    meta["messages"] = parse_body(body.lstrip("\n"))
    return meta


# ---------- markdown rendering ----------

def fmt_meta(msg: dict) -> str:
    bits = []
    if msg["reactions"]:
        bits.append("reactions: " + ", ".join(msg["reactions"]))
    if msg["files"]:
        bits.append("attached: " + ", ".join(msg["files"]))
    return f"_({' · '.join(bits)})_" if bits else ""


def oneliner(text: str, limit: int = 70) -> str:
    first = next((l.strip() for l in text.splitlines() if l.strip()), "")
    first = first.strip("*").strip()
    return first if len(first) <= limit else first[:limit].rstrip() + "…"


def stamp(msg: dict, parent_date: str | None) -> str:
    if msg["date"] and msg["date"] != parent_date:
        return f"{msg['date']} {msg['time']}"
    return msg["time"] or ""


def render_message(msg: dict, parent_date: str | None, reply: bool) -> list[str]:
    out: list[str] = []
    when = stamp(msg, parent_date)
    if reply:
        out.append(f"  ↳ **{msg['name']}** · {when}")
        for ln in msg["text"].splitlines() or [""]:
            out.append(f"  {ln}".rstrip())
        meta = fmt_meta(msg)
        if meta:
            out.append(f"  {meta}")
    else:
        out.append(f"**{msg['name']}** · {msg['date']} {msg['time']}")
        out.extend(msg["text"].splitlines())
        meta = fmt_meta(msg)
        if meta:
            out.append(meta)
    return out


def render_thread(msgs: list[dict]) -> tuple[str, list[str]]:
    """Return (parent_oneliner, body_lines)."""
    parent = msgs[0]
    pdate = parent["date"]
    lines = render_message(parent, pdate, reply=False)
    for m in msgs[1:]:
        lines.append("")
        lines.extend(render_message(m, pdate, reply=True))
    if len(msgs) == 1:
        lines += ["", "_(No replies in thread.)_"]
    return oneliner(parent["text"]), lines


def render_involved(meta: dict) -> tuple[str, str]:
    msgs = meta["messages"]
    one, body = render_thread(msgs)
    if meta.get("kind") == "dm":
        title = f'# DM with {meta.get("peer", "?")} — "{one}"'
    else:
        title = f'# #{meta.get("channel_name", "?")} — {one}'
    head = [title, "", meta.get("permalink", ""), ""]
    return meta["slug"], "\n".join(head + body) + "\n"


def render_by_jira(meta: dict) -> tuple[str, str]:
    key = meta["jira_key"]
    msgs = meta["messages"]
    one, body = render_thread(msgs)
    ch = meta.get("channel_name", "?")
    head = [
        f"# {key} — Slack discussion",
        "",
        f"Jira ticket: [../../jira/{key}/ticket.md](../../jira/{key}/ticket.md)",
        meta.get("jira_summary", ""),
        "",
        "---",
        "",
        f"## #{ch} — {one}",
        "",
        meta.get("permalink", ""),
        "",
    ]
    return key, "\n".join(head + body) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--comms", default="./comms", type=Path,
                    help="comms root (default ./comms)")
    args = ap.parse_args()
    raw_dir = args.comms / "slack" / "_raw"
    if not raw_dir.is_dir():
        print(f"no raw dir: {raw_dir}", file=sys.stderr)
        return 1
    inv_dir = args.comms / "slack" / "involved"
    jira_dir = args.comms / "slack" / "by-jira"
    n_inv = n_jira = 0
    for raw in sorted(raw_dir.glob("*.txt")):
        meta = parse_raw(raw)
        if not meta.get("messages"):
            print(f"skip (no messages parsed): {raw.name}", file=sys.stderr)
            continue
        if meta.get("category") == "by-jira":
            name, md = render_by_jira(meta)
            jira_dir.mkdir(parents=True, exist_ok=True)
            (jira_dir / f"{name}.md").write_text(md, encoding="utf-8")
            n_jira += 1
        else:
            name, md = render_involved(meta)
            inv_dir.mkdir(parents=True, exist_ok=True)
            (inv_dir / f"{name}.md").write_text(md, encoding="utf-8")
            n_inv += 1
        print(f"rendered: {name}", file=sys.stderr)
    print(f"Slack: {n_inv} involved + {n_jira} by-jira → {args.comms/'slack'}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
