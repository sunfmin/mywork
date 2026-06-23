#!/usr/bin/env python3
"""Render Slack thread Markdown from raw MCP / slackdump output.

The Slack step of mywork separates *fetch* from *format*. Whichever path fetched
(agent + MCP, or headless slackdump) writes each thread verbatim to a raw file;
this script deterministically renders the Markdown. Raw output is the single
source of truth in ``_raw/``; the ``.md`` files are derived (idempotent, no model
tokens). Output is tuned for an AI reader: YAML front-matter for triage, an
index manifest, full timestamps with a declared tz, an explicit owner marker,
and normalized noise (emoji skin-tones, meeting-invite boilerplate, attachments).

Raw file format (``comms/slack/_raw/<name>.txt``)::

    @@meta
    slug: dm-xuxin-20260623-1782201646     # output basename (involved only)
    kind: dm                               # dm | channel
    peer: Xu Xin                           # other DM participant (dm only)
    channel_name: ai-coe                   # channel name w/o # (channel only)
    channel_id: D82B0SG9M
    thread_ts: 1782201646.318239
    permalink: https://...
    category: involved                     # involved | by-jira
    owner: felix                           # the authenticated user (marks "(me)")
    jira_key: ISMS-610                     # by-jira only
    jira_summary: [RISK25] ...             # by-jira only
    @@body
    === THREAD PARENT MESSAGE ===
    ... (verbatim slack_read_thread / slackdump output) ...

Usage:
    render-slack.py [--comms ./comms] [--owner NAME] [--tz +08:00]
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
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
SKIN = re.compile(r"::skin-tone-\d+")
TENCENT = re.compile(r"https://meeting\.tencent\.com/\S+")
JIRA_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


# ---------- text normalization ----------

def clean_text(s: str) -> str:
    """Resolve Slack markup (<@U|name>, <!here>, <url|text>), entities, skin tones."""
    def angle(m: re.Match) -> str:
        inner = m.group(1)
        if inner.startswith("@"):
            body = inner[1:]
            return "@" + (body.split("|", 1)[1] if "|" in body else body)
        if inner.startswith("!"):
            body = inner[1:]
            if "|" in body:
                return "@" + body.split("|", 1)[1]
            return "@subteam" if body.startswith("subteam^") else "@" + body
        if "|" in inner:
            url, text = inner.split("|", 1)
            return text if url.startswith(("tel:", "mailto:")) else f"[{text}]({url})"
        return inner
    return SKIN.sub("", html.unescape(re.sub(r"<([^>]+)>", angle, s)))


def collapse_invite(text: str) -> str:
    """Tencent-meeting invite boilerplate -> one line (drops ~6 lines of noise)."""
    if ("腾讯会议" in text and "meeting.tencent.com" in text
            and any(k in text for k in ("点击链接", "邀请您参加", "复制该信息"))):
        m = TENCENT.search(text)
        return f"[腾讯会议邀请] {m.group(0)}" if m else "[腾讯会议邀请]"
    return text


def parse_files(raw: str) -> list[tuple[str, str]]:
    """'a.png (ID: F1, image/png, 1KB), b.xlsx (ID: F2, app/..., 2KB)' -> [(name, mime)]."""
    return [(m.group(1).strip(), m.group(2).strip())
            for m in re.finditer(r"([^,(]+?)\s*\(ID:\s*[^,]*,\s*([^,]+?)\s*,", raw)]


# ---------- parsing ----------

def parse_block(lines: list[str]) -> dict | None:
    name = date = time = None
    text_lines: list[str] = []
    reactions: list[str] = []
    files: list[tuple[str, str]] = []
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
            reactions.append(SKIN.sub("", m.group(1).strip()))
            continue
        if (m := FILES_RE.match(ln)):
            files.extend(parse_files(m.group(1)))
            continue
        if any(n in ln for n in NOISE):
            continue
        text_lines.append(ln)
    if name is None:
        return None
    text = collapse_invite(clean_text("\n".join(text_lines).strip("\n")).rstrip())
    return {"name": name, "date": date, "time": time, "text": text,
            "reactions": reactions, "files": files}


def parse_body(body: str) -> list[dict]:
    lines = body.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == PARENT_MARK)
    except StopIteration:
        return []
    lines = lines[start + 1:]
    rep_idx = next((i for i, l in enumerate(lines) if REPLIES_MARK.match(l)), None)
    blocks = [parse_block(lines[:rep_idx] if rep_idx is not None else lines)]
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
    meta: dict = {}
    for ln in meta_part.splitlines():
        if ln.strip() in ("", "@@meta"):
            continue
        k, _, v = ln.partition(":")
        meta[k.strip()] = v.strip()
    meta["messages"] = parse_body(body.lstrip("\n"))
    return meta


# ---------- rendering ----------

def yv(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def fmt_meta_line(msg: dict) -> str:
    bits = []
    if msg["reactions"]:
        bits.append("reactions: " + ", ".join(msg["reactions"]))
    for name, mime in msg["files"]:
        bits.append(f"[{'image' if mime.startswith('image/') else 'file'}: {name}]")
    return f"_({' · '.join(bits)})_" if bits else ""


def oneliner(text: str, limit: int = 80) -> str:
    first = next((l.strip() for l in text.splitlines() if l.strip()), "")
    first = first.strip("*").strip()
    return first if len(first) <= limit else first[:limit].rstrip() + "…"


def author(msg: dict, owner: str | None) -> str:
    return f"{msg['name']} (me)" if owner and msg["name"] == owner else msg["name"]


def render_message(msg: dict, owner: str | None, reply: bool) -> list[str]:
    when = f"{msg['date']} {msg['time']}"
    out: list[str] = []
    if reply:
        out.append(f"  ↳ **{author(msg, owner)}** · {when}")
        for ln in msg["text"].splitlines() or [""]:
            out.append(f"  {ln}".rstrip())
        if (m := fmt_meta_line(msg)):
            out.append(f"  {m}")
    else:
        out.append(f"**{author(msg, owner)}** · {when}")
        out.extend(msg["text"].splitlines())
        if (m := fmt_meta_line(msg)):
            out.append(m)
    return out


def render_thread_body(msgs: list[dict], owner: str | None) -> list[str]:
    lines = render_message(msgs[0], owner, reply=False)
    for m in msgs[1:]:
        lines.append("")
        lines.extend(render_message(m, owner, reply=True))
    if len(msgs) == 1:
        lines += ["", "_(No replies in thread.)_"]
    return lines


def front_matter(fm: dict) -> str:
    return "---\n" + "".join(f"{k}: {yv(v)}\n" for k, v in fm.items()) + "---\n"


def build(meta: dict, owner: str | None, tz: str, keys: list[str]) -> tuple[str, str, dict]:
    """Return (output_subpath_basename, markdown, index_record)."""
    msgs = meta["messages"]
    parent = msgs[0]
    cat = meta.get("category", "involved")
    o = meta.get("owner") or owner
    participants, seen = [], set()
    for m in msgs:
        if m["name"] not in seen:
            seen.add(m["name"])
            participants.append(m["name"])
    text_all = "\n".join(m["text"] for m in msgs)
    jira = sorted({k for k in keys if re.search(rf"\b{re.escape(k)}\b", text_all)})
    if cat == "by-jira" and meta.get("jira_key"):
        jira = sorted(set(jira) | {meta["jira_key"]})
    chan = (f"DM with {meta.get('peer', '?')}" if meta.get("kind") == "dm"
            else f"#{meta.get('channel_name', '?')}")
    gist = oneliner(parent["text"]) or "(no text)"

    fm = {"kind": meta.get("kind", ""), "channel": chan,
          "participants": participants}
    if o:
        fm["owner"] = o
    fm["category"] = cat
    fm["date_start"] = f"{parent['date']}T{parent['time']}{tz}"
    fm["date_end"] = f"{msgs[-1]['date']}T{msgs[-1]['time']}{tz}"
    fm["tz"] = tz
    fm["messages"] = len(msgs)
    fm["jira"] = jira
    fm["permalink"] = meta.get("permalink", "")

    body = render_thread_body(msgs, o)
    rec = {"kind": meta.get("kind", ""), "channel": chan, "category": cat,
           "participants": participants, "date_start": fm["date_start"],
           "date_end": fm["date_end"], "messages": len(msgs), "jira": jira,
           "gist": gist, "permalink": fm["permalink"]}

    if cat == "by-jira":
        key = meta["jira_key"]
        head = [f"# {key} — Slack discussion", "",
                f"Jira ticket: [../../jira/{key}/ticket.md](../../jira/{key}/ticket.md)",
                meta.get("jira_summary", ""), "", "---", "",
                f"## {chan} — {gist}", ""]
        rec["file"] = f"by-jira/{key}.md"
        return f"by-jira/{key}", front_matter(fm) + "\n".join(head + body) + "\n", rec
    slug = meta["slug"]
    title = (f'# {chan} — "{gist}"' if meta.get("kind") == "dm"
             else f"# {chan} — {gist}")
    head = [title, "", meta.get("permalink", ""), ""]
    rec["file"] = f"involved/{slug}.md"
    return f"involved/{slug}", front_matter(fm) + "\n".join(head + body) + "\n", rec


def write_index(slack: Path, recs: list[dict]) -> None:
    recs = sorted(recs, key=lambda r: r["date_end"], reverse=True)
    (slack / "_index.json").write_text(
        json.dumps(recs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Slack comms index",
        "",
        "Machine-readable sibling: `_index.json`. Raw source of truth: `_raw/*.txt`",
        "(the `.md` files are derived — re-render, don't hand-edit). Threads are",
        "newest-first; open only the rows you need.",
        "",
        "| date | channel | participants | msgs | jira | gist | file |",
        "|------|---------|--------------|------|------|------|------|",
    ]
    for r in recs:
        d = r["date_start"][:10]
        d2 = r["date_end"][:10]
        span = d if d == d2 else f"{d}→{d2[5:]}"
        who = ", ".join(r["participants"][:4]) + ("…" if len(r["participants"]) > 4 else "")
        jira = " ".join(r["jira"]) or "—"
        gist = r["gist"].replace("|", "\\|")
        lines.append(f"| {span} | {r['channel']} | {who} | {r['messages']} | "
                     f"{jira} | {gist} | [{r['file'].split('/')[-1]}]({r['file']}) |")
    (slack / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--comms", default="./comms", type=Path)
    ap.add_argument("--owner", default=None, help="authenticated user display name")
    ap.add_argument("--tz", default=None, help="tz offset, e.g. +08:00 (default: local)")
    args = ap.parse_args()
    raw_dir = args.comms / "slack" / "_raw"
    if not raw_dir.is_dir():
        print(f"no raw dir: {raw_dir}", file=sys.stderr)
        return 1
    tz = args.tz or (lambda z: (z[:3] + ":" + z[3:]) if z else "+00:00")(
        datetime.now().astimezone().strftime("%z"))
    keyfile = args.comms / "jira" / "_keys.txt"
    keys = ([k.strip() for k in keyfile.read_text().splitlines() if k.strip()]
            if keyfile.exists() else [])
    slack = args.comms / "slack"
    recs: list[dict] = []
    n_inv = n_jira = 0
    for raw in sorted(raw_dir.glob("*.txt")):
        meta = parse_raw(raw)
        if not meta.get("messages"):
            print(f"skip (no messages): {raw.name}", file=sys.stderr)
            continue
        sub, md, rec = build(meta, args.owner, tz, keys)
        out = slack / f"{sub}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        recs.append(rec)
        if rec["category"] == "by-jira":
            n_jira += 1
        else:
            n_inv += 1
        print(f"rendered: {sub}", file=sys.stderr)
    if recs:
        write_index(slack, recs)
    print(f"Slack: {n_inv} involved + {n_jira} by-jira (+ index) → {slack}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
