#!/usr/bin/env python3
"""
litedocs-history - search what a git-backed docs repo USED to say.

Companion to litedocs-mcp. Where that server searches the working tree, this
one searches every version of every page that has ever existed, so you can ask
"when did the wiki say X, and when did it stop saying it?" — the question a
plain grep cannot answer, because the text is gone.

Built for GitHub wikis, which are ordinary git repos:

    git clone https://github.com/OWNER/REPO.wiki.git

Stdlib only, plus the `git` binary. No network, no embeddings.

    # build the history index (separate file from the live index)
    python3 litedocs_history.py --repo ~/work/project.wiki --index

    # find text that no longer exists
    python3 litedocs_history.py --repo ~/work/project.wiki --search "settlement window"

    # exact provenance for a known phrase (git pickaxe)
    python3 litedocs_history.py --repo ~/work/project.wiki --when "three business days"

    # every revision of one page
    python3 litedocs_history.py --repo ~/work/project.wiki --page Home.md

    # as an MCP server
    python3 litedocs_history.py --repo ~/work/project.wiki
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys

try:
    from litedocs_mcp import split_sections, DEFAULT_EXTS
except ImportError:  # running from a directory where the sibling module is not importable
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from litedocs_mcp import split_sections, DEFAULT_EXTS

VERSION = "0.1.0"
MAX_BLOB_BYTES = 2 * 1024 * 1024
NULL_SHA = "0" * 40


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------

def git(repo, *args, **kw):
    """Run a git command and return stdout as text. Never uses a shell."""
    out = subprocess.run(["git", "-C", repo] + list(args),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         check=False)
    if out.returncode != 0 and not kw.get("allow_fail"):
        raise RuntimeError("git %s failed: %s"
                           % (" ".join(args[:2]), out.stderr.decode("utf-8", "replace").strip()))
    return out.stdout.decode("utf-8", "replace")


def check_repo(repo):
    if not os.path.isdir(os.path.join(repo, ".git")) and not os.path.isdir(os.path.join(repo, "objects")):
        raise SystemExit(
            "Not a git repository: %s\n"
            "For a GitHub wiki, clone it first:\n"
            "  git clone https://github.com/OWNER/REPO.wiki.git" % repo)


RAW = re.compile(r"^:\d+ \d+ ([0-9a-f]+) ([0-9a-f]+) ([A-Z])(\d*)\t(.*)$")


def read_events(repo, exts):
    """Every edit to a documentation file, oldest first.

    Returns a list of dicts: commit, date, author, path, blob, status.
    Renames are followed (-M), so a page that was retitled keeps one timeline
    under its newest name. Merge commits are skipped, as `git log --raw` does
    by default — wiki histories are linear in practice.
    """
    fmt = "\x01%H\x1f%aI\x1f%an\x1f%s"
    raw = git(repo, "log", "--all", "--reverse", "-M", "--raw", "--no-abbrev",
              "--date-order", "--format=" + fmt)

    events, renames = [], []
    commit = date = author = subject = None
    for line in raw.splitlines():
        if line.startswith("\x01"):
            parts = line[1:].split("\x1f")
            if len(parts) == 4:
                commit, date, author, subject = parts
            continue
        m = RAW.match(line)
        if not m or commit is None:
            continue
        _old, new, status, _score, rest = m.groups()
        if status == "R":
            old_path, path = rest.split("\t", 1)
            renames.append({"old": old_path, "new": path, "date": date, "commit": commit})
        else:
            path = rest
        if os.path.splitext(path)[1].lower() not in exts:
            continue
        events.append({"commit": commit, "date": date, "author": author,
                       "subject": subject, "path": path,
                       "blob": None if status == "D" else new,
                       "status": status})
    return events, renames


def read_blobs(repo, shas):
    """Fetch many blobs in one `git cat-file --batch` call."""
    shas = [s for s in shas if s and s != NULL_SHA]
    if not shas:
        return {}
    proc = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    out, _ = proc.communicate(("\n".join(shas) + "\n").encode())

    blobs, pos = {}, 0
    while pos < len(out):
        nl = out.find(b"\n", pos)
        if nl == -1:
            break
        header = out[pos:nl].decode("utf-8", "replace").split()
        pos = nl + 1
        if len(header) < 3:          # "<sha> missing"
            continue
        sha, _kind, size = header[0], header[1], int(header[2])
        body = out[pos:pos + size]
        pos += size + 1              # trailing newline
        if size <= MAX_BLOB_BYTES and b"\0" not in body[:8000]:
            blobs[sha] = body.decode("utf-8", "replace")
    return blobs


# --------------------------------------------------------------------------
# index
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS renames (
    old_path TEXT, new_path TEXT, date TEXT, commit_sha TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS versions USING fts5(
    path UNINDEXED,
    breadcrumb,
    body,
    live_from UNINDEXED,
    added_by UNINDEXED,
    added_commit UNINDEXED,
    live_until UNINDEXED,
    removed_by UNINDEXED,
    removed_commit UNINDEXED,
    start_line UNINDEXED,
    end_line UNINDEXED,
    tokenize = 'porter unicode61'
);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
    except sqlite3.OperationalError as e:
        if "fts5" in str(e).lower():
            raise SystemExit("This Python's SQLite was built without FTS5.")
        raise
    return conn


def build(conn, repo, exts, force=False):
    """Rebuild the history index.

    The expensive-sounding part — reading every version of every page — is
    cheap, because git stores one blob per distinct content. A page edited
    fifty times contributes fifty blobs, not fifty times the repo.

    The useful part is collapsing: a section that survives twenty edits to
    other parts of its page becomes ONE row spanning the whole period it was
    live, not twenty near-identical hits.
    """
    head = git(repo, "rev-parse", "--all").strip()
    # Must be stable ACROSS processes: Python's hash() is randomised per run.
    stamp = hashlib.sha256(head.encode()).hexdigest()
    row = conn.execute("SELECT value FROM state WHERE key='refs'").fetchone()
    if row and row["value"] == stamp and not force:
        return {"skipped": True,
                "sections": conn.execute("SELECT count(*) FROM versions").fetchone()[0]}

    events, renames = read_events(repo, exts)
    if not events:
        return {"skipped": False, "sections": 0, "paths": 0, "events": 0}

    rename_map = {r["old"]: r["new"] for r in renames}

    def canonical(path, seen=None):
        seen = seen or set()
        while path in rename_map and path not in seen:
            seen.add(path)
            path = rename_map[path]
        return path

    by_path = {}
    for ev in events:
        by_path.setdefault(canonical(ev["path"]), []).append(ev)

    blobs = read_blobs(repo, {ev["blob"] for ev in events})

    conn.execute("DELETE FROM versions")
    conn.execute("DELETE FROM renames")
    conn.executemany(
        "INSERT INTO renames (old_path, new_path, date, commit_sha) VALUES (?,?,?,?)",
        [(r["old"], r["new"], r["date"], r["commit"]) for r in renames])

    rows = []
    for path, evs in by_path.items():
        evs.sort(key=lambda e: (e["date"], e["commit"]))
        open_chunks = {}   # (breadcrumb, body) -> record

        for ev in evs:
            if ev["blob"] is None or ev["blob"] not in blobs:
                present = {}
            else:
                present = {(bc, body): (sl, el)
                           for bc, sl, el, body in
                           split_sections(blobs[ev["blob"]], path)}

            for key, (sl, el) in present.items():
                if key in open_chunks:
                    rec = open_chunks[key]
                    rec["start_line"], rec["end_line"] = sl, el
                else:
                    open_chunks[key] = {
                        "live_from": ev["date"], "added_by": ev["author"],
                        "added_commit": ev["commit"],
                        "start_line": sl, "end_line": el}

            for key in [k for k in open_chunks if k not in present]:
                rec = open_chunks.pop(key)
                rows.append((path, key[0], key[1], rec["live_from"], rec["added_by"],
                             rec["added_commit"], ev["date"], ev["author"], ev["commit"],
                             rec["start_line"], rec["end_line"]))

        for key, rec in open_chunks.items():      # still live at HEAD
            rows.append((path, key[0], key[1], rec["live_from"], rec["added_by"],
                         rec["added_commit"], None, None, None,
                         rec["start_line"], rec["end_line"]))

    conn.executemany(
        "INSERT INTO versions (path, breadcrumb, body, live_from, added_by, "
        "added_commit, live_until, removed_by, removed_commit, start_line, end_line) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("INSERT INTO state (key, value) VALUES ('refs', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (stamp,))
    conn.commit()
    return {"skipped": False, "sections": len(rows), "paths": len(by_path),
            "events": len(events), "renames": len(renames),
            "removed": sum(1 for r in rows if r[6] is not None)}


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def _match(query, joiner):
    tokens = TOKEN.findall(query)
    return (" %s " % joiner).join('"%s"' % t for t in tokens) if tokens else None


def search(conn, query, limit=5, only_removed=False):
    phrases = re.findall(r'"([^"]+)"', query)
    attempts = []
    if phrases:
        attempts.append(" AND ".join('"%s"' % p for p in phrases))
    attempts += [_match(query, "AND"), _match(query, "OR")]
    where = " AND live_until IS NOT NULL" if only_removed else ""

    for match in attempts:
        if not match:
            continue
        try:
            rows = conn.execute(
                "SELECT path, breadcrumb, live_from, added_by, added_commit, "
                "       live_until, removed_by, removed_commit, "
                "       snippet(versions, 2, '**', '**', ' … ', 40) AS snip, "
                "       bm25(versions, 0,10,1,0,0,0,0,0,0,0,0) AS score "
                "FROM versions WHERE versions MATCH ?" + where +
                " ORDER BY score LIMIT ?", (match, limit)).fetchall()
        except sqlite3.OperationalError:
            continue
        if rows:
            return rows
    return []


def day(iso):
    return iso[:10] if iso else None


def format_results(rows, query):
    if not rows:
        return ("No version of any page matched %r — not now, not in the history."
                % query)
    out = ["%d match(es) for %r across the full history:" % (len(rows), query), ""]
    for r in rows:
        if r["live_until"]:
            status = "REMOVED %s by %s (%s)" % (
                day(r["live_until"]), r["removed_by"], r["removed_commit"][:8])
        else:
            status = "still current"
        out.append("%s — %s" % (r["path"], r["breadcrumb"]))
        out.append("  added  %s by %s (%s)" % (
            day(r["live_from"]), r["added_by"], r["added_commit"][:8]))
        out.append("  status %s" % status)
        out.append("  %s" % " ".join(r["snip"].split()))
        out.append("")
    out.append("Confirm exact provenance with when_documented on a literal phrase "
               "from one of these.")
    return "\n".join(out)


# --------------------------------------------------------------------------
# pickaxe: exact provenance for a known phrase
# --------------------------------------------------------------------------

def occurrences(repo, rev, text, path=None):
    args = ["grep", "-c", "-F", "-e", text, rev]
    if path:
        args += ["--", path]
    out = git(repo, *args, allow_fail=True)
    total = 0
    for line in out.splitlines():
        try:
            total += int(line.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
    return total


def when_documented(repo, text, path=None, limit=20):
    """Wrap `git log -S` and label each hit as an addition or a removal."""
    args = ["log", "--all", "-S", text, "--no-abbrev",
            "--format=%H\x1f%aI\x1f%an\x1f%s", "-n", str(limit)]
    if path:
        args += ["--", path]
    out = git(repo, *args, allow_fail=True).strip()
    if not out:
        return ("No commit ever added or removed that exact text.\n"
                "It is a literal match — check wording, or use search_history "
                "to find the phrasing that was actually used.")

    lines = []
    for raw in out.splitlines():
        parts = raw.split("\x1f")
        if len(parts) != 4:
            continue
        sha, date, author, subject = parts
        here = occurrences(repo, sha, text, path)
        parent = git(repo, "rev-parse", sha + "^", allow_fail=True).strip()
        before = occurrences(repo, parent, text, path) if parent and "^" not in parent else 0
        verb = "ADDED" if here > before else "REMOVED" if here < before else "CHANGED"
        lines.append("%-8s %s  %-20s %s  %s"
                     % (verb, day(date), author[:20], sha[:8], subject[:60]))

    lines.sort(key=lambda s: s[9:19])
    return ("Commits that changed the presence of %r:\n\n" % text) + "\n".join(lines)


def page_history(repo, path, limit=30):
    out = git(repo, "log", "--all", "--follow", "--format=%aI\x1f%an\x1f%H\x1f%s",
              "-n", str(limit), "--", path, allow_fail=True).strip()
    if not out:
        return "No history for %s. Check the path — list it with search_history." % path
    lines = ["Revisions of %s (newest first):" % path, ""]
    for raw in out.splitlines():
        parts = raw.split("\x1f")
        if len(parts) == 4:
            date, author, sha, subject = parts
            lines.append("%s  %-20s %s  %s" % (day(date), author[:20], sha[:8], subject[:60]))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "search_history",
        "description": (
            "Search every version of every page that has ever existed in the docs "
            "repo, including text that has since been deleted. Use this when the "
            "wording you are looking for is NOT in the current docs, or when you "
            "need to know for what period the docs said something. Returns the "
            "section, when it was added and by whom, and when it was removed and "
            "by whom. Pass 3-6 distinctive keywords, not a sentence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords to search for."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "only_removed": {"type": "boolean",
                                 "description": "Only text that no longer exists."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "when_documented",
        "description": (
            "Exact provenance for a LITERAL phrase: which commits added it and "
            "which removed it, with author and date. This is git's pickaxe, so "
            "the text must match character for character — use search_history "
            "first to find the real wording, then confirm it here."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Exact text, as written."},
                "path": {"type": "string", "description": "Optional: restrict to one page."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "page_history",
        "description": "Every revision of one page, newest first, following renames.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"},
                           "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            "required": ["path"],
        },
    },
]


def text_result(text):
    return {"content": [{"type": "text", "text": text}]}


def serve(conn, repo, exts):
    log("litedocs-history %s serving %s" % (VERSION, repo))
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method, req_id, params = req.get("method"), req.get("id"), req.get("params") or {}

        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "litedocs-history", "version": VERSION}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        elif method == "prompts/list":
            result = {"prompts": []}
        elif method == "ping":
            result = {}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"query": args}
            if not isinstance(args, dict):
                args = {}
            try:
                if name == "search_history":
                    q = (args.get("query") or "").strip()
                    result = text_result(format_results(
                        search(conn, q, int(args.get("limit") or 5),
                               bool(args.get("only_removed"))), q))
                elif name == "when_documented":
                    result = text_result(when_documented(
                        repo, args.get("text") or "", args.get("path")))
                elif name == "page_history":
                    result = text_result(page_history(
                        repo, args.get("path") or "", int(args.get("limit") or 30)))
                else:
                    result = text_result(
                        "This server has no tool named %r. It provides exactly: %s."
                        % (name, ", ".join(t["name"] for t in TOOLS)))
            except Exception as e:
                result = text_result("Tool error: %s" % e)
                result["isError"] = True
        elif method is not None and req_id is None:
            continue
        else:
            if req_id is None:
                continue
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id,
                                         "error": {"code": -32601,
                                                   "message": "Method not found: %s" % method}}) + "\n")
            sys.stdout.flush()
            continue

        if req_id is not None:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id,
                                         "result": result}) + "\n")
            sys.stdout.flush()


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="Clone of the docs/wiki git repo.")
    ap.add_argument("--db", help="Index file (default: <repo>/.litedocs-history.sqlite3).")
    ap.add_argument("--exts", default=",".join(DEFAULT_EXTS))
    ap.add_argument("--index", action="store_true", help="Build the index and exit.")
    ap.add_argument("--force", action="store_true", help="Rebuild even if refs are unchanged.")
    ap.add_argument("--search", metavar="QUERY")
    ap.add_argument("--only-removed", action="store_true")
    ap.add_argument("--when", metavar="TEXT", help="Pickaxe a literal phrase.")
    ap.add_argument("--page", metavar="PATH", help="Revisions of one page.")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    repo = os.path.abspath(os.path.expanduser(args.repo))
    check_repo(repo)
    exts = {e if e.startswith(".") else "." + e
            for e in (x.strip().lower() for x in args.exts.split(",")) if e}
    db_path = args.db or os.path.join(repo, ".litedocs-history.sqlite3")

    if args.when:
        print(when_documented(repo, args.when, limit=max(args.limit, 20)))
        return
    if args.page:
        print(page_history(repo, args.page, max(args.limit, 30)))
        return

    conn = connect(db_path)
    stats = build(conn, repo, exts, force=args.force)

    if args.index:
        if stats.get("skipped"):
            print("Already up to date: %d sections indexed." % stats["sections"])
        else:
            print("Indexed %(events)d edits across %(paths)d pages into %(sections)d "
                  "section-lifetimes (%(removed)d no longer current, "
                  "%(renames)d renames followed)." % stats)
        print("Index: %s" % db_path)
        return
    if args.search:
        print(format_results(
            search(conn, args.search, args.limit, args.only_removed), args.search))
        return

    log("history index: %s sections" % stats.get("sections"))
    serve(conn, repo, exts)


if __name__ == "__main__":
    main()
