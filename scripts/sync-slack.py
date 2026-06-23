#!/usr/bin/env python3
"""Headless Slack sync via slackdump — the no-agent path for mywork.

If `slackdump` (https://github.com/rusq/slackdump) is installed and a workspace
is authenticated, this fetches everything without an in-session MCP client:

    search (from:me / to:me / each Jira key, bounded by after:<date>)
      -> read slackdump.sqlite SEARCH_MESSAGE for (channel, permalink)
      -> thread root = ?thread_ts= in the permalink, else the message ts
    dump each unique <channel>:<root> to JSON
    convert -> comms/slack/_raw/*.txt   (same format the agent path writes)
    render  -> render-slack.py          (the single renderer both paths share)

Exit codes:
    0  success
    2  slackdump not installed  -> caller should use the agent path
    3  slackdump installed but no authenticated workspace

Usage:
    sync-slack.py --comms ./comms [--days 14]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RENDER = HERE / "render-slack.py"


def have_slackdump() -> str | None:
    return shutil.which("slackdump")


def authed() -> bool:
    r = subprocess.run(["slackdump", "workspace", "list"],
                       capture_output=True, text=True)
    # "no authenticated workspaces" -> stderr error; a listed workspace -> stdout.
    return r.returncode == 0 and "=>" in (r.stdout or "")


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


# ---------- users ----------

def load_users(workdir: Path) -> tuple[dict[str, str], set[str]]:
    """Return (id -> display name, set of bot/deleted ids). Cached by slackdump."""
    run(["slackdump", "list", "users", "-format", "json", "-q"], cwd=workdir)
    files = list(workdir.glob("users-*.json"))
    if not files:
        return {}, set()
    arr = json.loads(files[0].read_text(encoding="utf-8"))
    names: dict[str, str] = {}
    bots: set[str] = set()
    for u in arr:
        uid = u.get("id")
        if not uid:
            continue
        prof = u.get("profile") or {}
        nm = (u.get("real_name") or prof.get("real_name")
              or prof.get("display_name") or u.get("name") or uid)
        names[uid] = (nm or uid).strip() or uid
        if u.get("is_bot") or u.get("deleted") or uid == "USLACKBOT":
            bots.add(uid)
    return names, bots


# ---------- search ----------

THREAD_TS_RE = re.compile(r"[?&]thread_ts=([0-9.]+)")


def search_threads(query: str, workdir: Path, idx: int) -> list[dict]:
    """Run one search; return [{cid, root, channel_name, permalink, host}]."""
    out = workdir / f"search_{idx}"
    out.mkdir(parents=True, exist_ok=True)
    r = run(["slackdump", "search", "messages", "-o", str(out), query])
    db = out / "slackdump.sqlite"
    if not db.exists():
        sys.stderr.write(f"  (search '{query}' produced no db)\n")
        return []
    import sqlite3
    con = sqlite3.connect(str(db))
    rows = con.execute(
        "SELECT CHANNEL_ID, CHANNEL_NAME, TS, CAST(DATA AS TEXT) FROM SEARCH_MESSAGE"
    ).fetchall()
    con.close()
    hits = []
    for cid, cname, ts, data in rows:
        try:
            d = json.loads(data)
        except Exception:
            d = {}
        link = d.get("permalink") or ""
        m = THREAD_TS_RE.search(link)
        root = m.group(1) if m else ts
        host = re.match(r"https?://([^/]+)/", link)
        hits.append({"cid": cid, "root": root, "channel_name": cname or "",
                     "permalink": link, "host": host.group(1) if host else None,
                     "data": d})   # full search message — fallback for standalone msgs
    return hits


def root_permalink(host: str | None, cid: str, root: str) -> str:
    if not host:
        return ""
    return f"https://{host}/archives/{cid}/p{root.replace('.', '')}?thread_ts={root}&cid={cid}"


# ---------- dump + convert ----------

def fmt_time(ts: str) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")


def enrich_mentions(text: str, users: dict[str, str]) -> str:
    """Rewrite raw <@UID> to <@UID|name> so render-slack.py resolves the name."""
    def um(m: re.Match) -> str:
        uid = m.group(1)
        nm = users.get(uid)
        return f"<@{uid}|{nm}>" if nm else m.group(0)
    return re.sub(r"<@([UW][A-Z0-9]+)>", um, text)


def msg_block(msg: dict, users: dict[str, str]) -> str:
    uid = msg.get("user") or ""
    name = users.get(uid, uid)
    lines = [f"From: {name} ({uid})",
             f"Time: {fmt_time(msg['ts'])}",
             f"Message TS: {msg['ts']}"]
    text = enrich_mentions(msg.get("text") or "", users)
    if text:
        lines.append(text)
    reactions = msg.get("reactions") or []
    if reactions:
        lines.append("Reactions: " + ", ".join(
            f"{r['name']} ({r['count']})" for r in reactions))
    files = msg.get("files") or []
    parts = [f"{f.get('name', 'file')} (ID: {f.get('id', '')}, "
             f"{f.get('mimetype', '')}, {f.get('size', '')} bytes)"
             for f in files if isinstance(f, dict)]
    if parts:
        lines.append("Files: " + ", ".join(parts))
    return "\n".join(lines)


def build_body(conv: dict, users: dict[str, str]) -> str:
    msgs = conv.get("messages") or []
    if not msgs:
        return ""
    parent, replies = msgs[0], msgs[1:]
    out = ["=== THREAD PARENT MESSAGE ===", msg_block(parent, users)]
    if replies:
        out += ["", f"=== THREAD REPLIES ({len(replies)} total) ===", ""]
        for i, r in enumerate(replies, 1):
            out += [f"--- Reply {i} of {len(replies)} ---", msg_block(r, users), ""]
    return "\n".join(out).rstrip() + "\n"


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def write_raw(raw_dir: Path, info: dict, conv: dict, users: dict[str, str]) -> None:
    cid, root = info["cid"], info["root"]
    kind = "dm" if cid.startswith("D") else "channel"
    date = datetime.fromtimestamp(float(root)).strftime("%Y%m%d")
    sec = root.split(".")[0]
    meta = {"kind": kind, "channel_id": cid, "thread_ts": root,
            "permalink": info["permalink"], "category": info["category"]}
    if kind == "dm":
        peer = users.get(info["channel_name"], info["channel_name"])
        meta["peer"] = peer
        stem = f"dm-{slugify(peer)}-{date}-{sec}"
    else:
        cname = conv.get("name") or info["channel_name"] or cid
        meta["channel_name"] = cname
        stem = f"{cname}-{date}-{sec}"
    if info["category"] == "by-jira":
        meta["jira_key"] = info["jira_key"]
        meta["jira_summary"] = info.get("jira_summary", "")
        stem = info["jira_key"]
    else:
        meta["slug"] = stem
    header = "@@meta\n" + "".join(f"{k}: {v}\n" for k, v in meta.items())
    body = build_body(conv, users)
    (raw_dir / f"{stem}.txt").write_text(header + "@@body\n" + body, encoding="utf-8")


def jira_summary(comms: Path, key: str) -> str:
    tk = comms / "jira" / key / "ticket.md"
    if not tk.exists():
        return ""
    for line in tk.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--comms", default="./comms", type=Path)
    ap.add_argument("--days", type=int, default=14,
                    help="involved window for from:me / to:me (default 14)")
    args = ap.parse_args()

    if not have_slackdump():
        sys.stderr.write("slackdump not installed — use the agent path.\n")
        return 2
    if not authed():
        sys.stderr.write("slackdump installed but no authenticated workspace.\n"
                         "Run: slackdump workspace new\n")
        return 3

    comms: Path = args.comms
    raw_dir = comms / "slack" / "_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    # slackdump run owns the slack output: regenerate clean (drop stale dumps/md).
    for old in raw_dir.glob("*.txt"):
        old.unlink()
    for sub in ("involved", "by-jira"):
        d = comms / "slack" / sub
        if d.is_dir():
            for old in d.glob("*.md"):
                old.unlink()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    keys = []
    keyfile = comms / "jira" / "_keys.txt"
    if keyfile.exists():
        keys = [k.strip() for k in keyfile.read_text().splitlines() if k.strip()]

    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        users, bots = load_users(workdir)

        # thread key (cid, root) -> info; involved first, then by-jira keys.
        threads: dict[tuple[str, str], dict] = {}
        queries = [("involved", None, f"from:me after:{cutoff}"),
                   ("involved", None, f"to:me after:{cutoff}")]
        queries += [("by-jira", k, k) for k in keys]

        for i, (category, jkey, query) in enumerate(queries):
            sys.stderr.write(f"search: {query}\n")
            for h in search_threads(query, workdir, i):
                if h["cid"].startswith("D") and h["channel_name"] in bots:
                    continue   # skip bot DMs (Jira/Kolide/Google/...)
                k = (h["cid"], h["root"], category, jkey)
                if k in threads:
                    continue
                info = {**h, "category": category}
                info["permalink"] = root_permalink(h["host"], h["cid"], h["root"])
                if category == "by-jira":
                    info["jira_key"] = jkey
                    info["jira_summary"] = jira_summary(comms, jkey)
                threads[k] = info

        if not threads:
            sys.stderr.write("no threads found.\n")
            return 0

        # Dump every unique (cid, root) once; reuse JSON across categories.
        uniq = {(t["cid"], t["root"]) for t in threads.values()}
        dumpdir = workdir / "dump"
        dumpdir.mkdir()
        ids = [f"{cid}:{root}" for cid, root in uniq]
        sys.stderr.write(f"dumping {len(ids)} thread(s)...\n")
        run(["slackdump", "dump", "-files=false", "-o", str(dumpdir), *ids])

        # `slackdump dump cid:ts` only emits a file when ts is a real thread
        # (has replies). Standalone messages produce nothing: for involved
        # those are just chat fragments (skip), but a by-jira hit IS the signal,
        # so synthesize a one-message conversation from the search payload.
        skipped = 0
        for info in threads.values():
            jf = dumpdir / f"{info['cid']}-{info['root']}.json"
            if jf.exists():
                conv = json.loads(jf.read_text(encoding="utf-8"))
            elif info["category"] == "by-jira" and info.get("data"):
                conv = {"name": info["channel_name"], "messages": [info["data"]]}
            else:
                skipped += 1
                continue
            write_raw(raw_dir, info, conv, users)
        if skipped:
            sys.stderr.write(f"skipped {skipped} standalone (non-thread) message(s)\n")

    # Single renderer, shared with the agent path.
    r = run([sys.executable, str(RENDER), "--comms", str(comms)])
    sys.stderr.write(r.stderr)
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
