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
import fnmatch
import json
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
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

def load_users(workdir: Path) -> tuple[dict[str, str], set[str], dict[str, str]]:
    """Return (id->name, bot/deleted ids, id->handle). handle = the display_name
    Slack indexes @-mentions under (used as the mention-search keyword)."""
    run(["slackdump", "list", "users", "-format", "json", "-q"], cwd=workdir)
    files = list(workdir.glob("users-*.json"))
    if not files:
        return {}, set(), {}
    arr = json.loads(files[0].read_text(encoding="utf-8"))
    names: dict[str, str] = {}
    handles: dict[str, str] = {}
    bots: set[str] = set()
    for u in arr:
        uid = u.get("id")
        if not uid:
            continue
        prof = u.get("profile") or {}
        nm = (u.get("real_name") or prof.get("real_name")
              or prof.get("display_name") or u.get("name") or uid)
        names[uid] = (nm or uid).strip() or uid
        handles[uid] = (prof.get("display_name") or u.get("real_name")
                        or u.get("name") or uid).strip() or uid
        if u.get("is_bot") or u.get("deleted") or uid == "USLACKBOT":
            bots.add(uid)
    return names, bots, handles


def load_member_channels(workdir: Path) -> set[str]:
    """IDs of channels/groups the authed user actually joined.

    Slack full-text search returns matches from PUBLIC channels you never
    joined (e.g. automated `#z-*` notification feeds whose bot username
    contains your name), so the mention search alone pulls in pure noise.
    We keep only joined channels. Returns an empty set on failure — callers
    must treat empty as "unknown, don't filter" so we never drop real data.
    """
    run(["slackdump", "list", "channels", "-format", "json", "-q"], cwd=workdir)
    files = list(workdir.glob("channels-*.json"))
    if not files:
        return set()
    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
    except Exception:
        return set()
    chans = data if isinstance(data, list) else (data.get("channels") or [])
    return {c["id"] for c in chans if c.get("is_member") and c.get("id")}


def member_channels_cached(comms: Path, workdir: Path, refresh: bool,
                           ttl_days: int = 7) -> set[str]:
    """The set of joined-channel IDs, cached locally so we don't pay the price.

    `slackdump list channels` walks the *entire* workspace channel list to learn
    which ones you're a member of — measured at 16-74s and rate-limit-prone, the
    single most expensive call in the sync. But the set of channels you've joined
    changes rarely, so we persist it to ``comms/slack/_member_channels.json`` and
    refresh at most once per ``ttl_days`` (or on ``--refresh-channels``). A failed
    refresh falls back to the stale cache rather than dropping the noise filter.
    """
    cache = comms / "slack" / "_member_channels.json"

    def read_cache() -> dict | None:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            return None

    if not refresh and (d := read_cache()) is not None:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(d["fetched"])
            if age < timedelta(days=ttl_days):
                return set(d.get("ids") or [])
        except Exception:
            pass

    ids = load_member_channels(workdir)
    if ids:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(
                {"fetched": datetime.now(timezone.utc).isoformat(),
                 "ids": sorted(ids)}), encoding="utf-8")
        except OSError:
            pass
        return ids
    # Fetch failed/empty — prefer a stale cache over losing the filter entirely.
    if (d := read_cache()) is not None:
        return set(d.get("ids") or [])
    return set()


# ---------- search ----------

THREAD_TS_RE = re.compile(r"[?&]thread_ts=([0-9.]+)")


def search_threads(query: str, workdir: Path, idx: int) -> list[dict]:
    """Run one search; return [{cid, root, channel_name, permalink, host}]."""
    out = workdir / f"search_{idx}"
    out.mkdir(parents=True, exist_ok=True)
    # -no-channel-users: skip fetching the member list of every channel a hit
    #   lands in (~2x faster on large result sets). We only read SEARCH_MESSAGE
    #   rows (channel id/name, ts, permalink, author) — never channel users —
    #   so this drops nothing we use. -files=false: don't download attachments.
    r = run(["slackdump", "search", "messages", "-no-channel-users",
             "-files=false", "-o", str(out), query])
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


def is_noise(conv: dict) -> bool:
    """A standalone hit whose only message has no body text and no files.

    These render as "(no text)" — content lives in `attachments`/`blocks`
    (app/webhook notifications), which build_body doesn't surface anyway.
    Only single-message convs qualify; real threads (with replies) never do.
    """
    msgs = conv.get("messages") or []
    if len(msgs) != 1:
        return False
    m = msgs[0]
    return not (m.get("text") or "").strip() and not (m.get("files") or [])


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def write_raw(raw_dir: Path, info: dict, conv: dict, users: dict[str, str],
              owner: str = "") -> None:
    cid, root = info["cid"], info["root"]
    kind = "dm" if cid.startswith("D") else "channel"
    date = datetime.fromtimestamp(float(root)).strftime("%Y%m%d")
    sec = root.split(".")[0]
    meta = {"kind": kind, "channel_id": cid, "thread_ts": root,
            "permalink": info["permalink"], "category": info["category"]}
    if owner:
        meta["owner"] = owner
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
    ap.add_argument("--jobs", type=int, default=5,
                    help="run this many slackdump searches concurrently "
                         "(default 5; each search is an independent process)")
    ap.add_argument("--refresh-channels", action="store_true",
                    help="force-refresh the cached joined-channels set "
                         "(otherwise reused for up to 7 days)")
    ap.add_argument("--exclude-channels", default="",
                    help="comma-separated channel-name globs to drop, e.g. "
                         "'china,*-cn,mpdm-*' (case-insensitive; like Jira's "
                         "--exclude-projects). Group DMs (群聊私信) are 'mpdm-*'.")
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
        # Warm the user cache serially (cheap, ~2s) so the concurrent search
        # fleet below reads it instead of racing to populate it. We need the
        # user map for name resolution regardless.
        users, bots, handles = load_users(workdir)

        threads: dict[tuple, dict] = {}
        member_channels: set[str] = set()
        exclude_pats = [p.strip().lower() for p in args.exclude_channels.split(",")
                        if p.strip()]

        def ingest(hits: list[dict], category: str, jkey, keep: bool) -> None:
            for h in hits:
                cid = h["cid"]
                cname = h.get("channel_name") or ""
                if exclude_pats and any(
                        fnmatch.fnmatch(cname.lower(), p) for p in exclude_pats):
                    continue   # explicitly excluded channel (--exclude-channels)
                if cid.startswith("D"):
                    if h["channel_name"] in bots:
                        continue   # skip bot DMs (Jira/Kolide/Google/...)
                elif member_channels and cid not in member_channels:
                    continue   # skip channels I never joined (search-only public hits)
                k = (cid, h["root"], category, jkey)
                if k in threads:
                    threads[k]["keep_standalone"] |= keep   # OR across queries
                    continue
                info = {**h, "category": category, "keep_standalone": keep}
                info["permalink"] = root_permalink(h["host"], h["cid"], h["root"])
                if category == "by-jira":
                    info["jira_key"] = jkey
                    info["jira_summary"] = jira_summary(comms, jkey)
                threads[k] = info

        # Every search is an independent slackdump process writing its own
        # search_<idx> dir, so fan them all out. Two ordering dependencies:
        #   - the mention-of-me search needs owner_handle, learned from from:me;
        #   - ingest()'s noise filter needs member_channels.
        # So: launch the identity-free searches + the (cached) channel fetch at
        # once, resolve from:me to fire the mention search, then ingest in a
        # deterministic order once every result is materialized.
        sys.stderr.write(
            f"searching: from:me, to:me, mention + {len(keys)} jira key(s) "
            f"({args.jobs} parallel)\n")
        fromme = owner = owner_handle = None
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
            # Joined-channels set is cached locally (refreshed ~weekly); when it
            # does hit the network its 16-74s cost overlaps the searches here.
            f_chans = ex.submit(member_channels_cached, comms, workdir,
                                args.refresh_channels)
            f_fromme = ex.submit(search_threads, f"from:me after:{cutoff}", workdir, 0)
            f_tome = ex.submit(search_threads, f"to:me after:{cutoff}", workdir, 1)
            f_jira = [(key, ex.submit(search_threads, key, workdir, 2 + n))
                      for n, key in enumerate(keys)]

            # from:me reveals who "me" is: owner name (for the "(me)" marker) and
            # owner handle (display_name — how Slack indexes @-mentions).
            fromme = f_fromme.result() or []
            owner = owner_handle = ""
            for h in fromme:
                if (uid := (h.get("data") or {}).get("user")):
                    owner, owner_handle = users.get(uid, ""), handles.get(uid, "")
                    break
            # mention-of-me catches channel posts that tag me but aren't "to" me.
            f_mention = (ex.submit(search_threads, f"{owner_handle} after:{cutoff}",
                                   workdir, 2 + len(keys)) if owner_handle else None)

            member_channels = f_chans.result()
            if member_channels:
                sys.stderr.write(f"member channels: {len(member_channels)} joined\n")

        # Ingest deterministically (futures are all complete after the pool
        # closes). from:me first — my own stray lines drop if standalone; to:me /
        # mention / jira keep standalone hits (someone else's signal to me).
        ingest(fromme, "involved", None, keep=False)
        ingest(f_tome.result() or [], "involved", None, keep=True)
        if f_mention is not None:
            ingest(f_mention.result() or [], "involved", None, keep=True)
        for key, fut in f_jira:
            ingest(fut.result() or [], "by-jira", key, keep=True)

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

        # dump cid:ts emits a file only for real threads (with replies). For a
        # standalone message we synthesize a one-message conv from the full search
        # payload — but only when worth keeping (mentions me / DM to me / Jira),
        # not my own stray one-liners.
        skipped = 0
        for info in threads.values():
            jf = dumpdir / f"{info['cid']}-{info['root']}.json"
            if jf.exists():
                conv = json.loads(jf.read_text(encoding="utf-8"))
            elif info.get("keep_standalone") and info.get("data"):
                conv = {"name": info["channel_name"], "messages": [info["data"]]}
            else:
                skipped += 1
                continue
            if is_noise(conv):
                skipped += 1
                continue   # "(no text)" app/webhook notification — nothing to render
            write_raw(raw_dir, info, conv, users, owner)
        if skipped:
            sys.stderr.write(f"skipped {skipped} of my own standalone line(s)\n")

    # Single renderer, shared with the agent path.
    r = run([sys.executable, str(RENDER), "--comms", str(comms)])
    sys.stderr.write(r.stderr)
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
