#!/usr/bin/env python3
"""
litedocs-mcp - a tiny, zero-dependency MCP server that makes a local folder
of Markdown / AsciiDoc documentation searchable by an AI agent.

Why it exists: an agentic CLI burns a lot of tokens grepping and reading whole
files. This exposes one precise `search_docs` tool backed by SQLite FTS5 (BM25),
chunked at heading boundaries, so the agent gets the right few sections in a
single call.

Stdlib only. No pip install, no model download, no network. Needs Python 3.9+
with SQLite compiled with FTS5 (true for virtually every CPython build).

    # one-off: build the index and try a query from the shell
    python3 litedocs_mcp.py --docs /path/to/repo/docs --index
    python3 litedocs_mcp.py --docs /path/to/repo/docs --search "retry policy"

    # as an MCP server (what the agent runs)
    python3 litedocs_mcp.py --docs /path/to/repo/docs
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys

VERSION = "0.1.0"
DEFAULT_EXTS = [".md", ".markdown", ".adoc", ".asciidoc", ".txt"]
MAX_CHUNK_CHARS = 2000
SKIP_DIRS = {".git", ".svn", "node_modules", "build", "target", "__pycache__",
             ".venv", "venv", ".idea", ".gradle", "dist", "out"}

MD_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
ADOC_HEADING = re.compile(r"^(={1,6})\s+(.*\S)\s*$")
FENCE = re.compile(r"^\s*(```|~~~|----|\.\.\.\.)")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------

def split_sections(text, path):
    """Split a document into (breadcrumb, start_line, end_line, body) sections.

    Headings are detected for Markdown (#) and AsciiDoc (=). Content inside
    fenced blocks is never treated as a heading. A section runs from its
    heading to the next heading at any level; oversized sections are split at
    paragraph boundaries so one huge page cannot swamp a result set.
    """
    lines = text.splitlines()
    is_adoc = os.path.splitext(path)[1].lower() in (".adoc", ".asciidoc")
    heading_re = ADOC_HEADING if is_adoc else MD_HEADING

    # Markdown files sometimes carry AsciiDoc-style rules and vice versa; try
    # the primary syntax and fall back if the document has no hits at all.
    marks = []          # (line_index, level, title)
    in_fence = False
    fence_token = None
    for i, line in enumerate(lines):
        m_fence = FENCE.match(line)
        if m_fence:
            token = m_fence.group(1)
            if not in_fence:
                in_fence, fence_token = True, token
            elif token == fence_token:
                in_fence, fence_token = False, None
            continue
        if in_fence:
            continue
        m = heading_re.match(line)
        if m:
            marks.append((i, len(m.group(1)), m.group(2).strip()))

    if not marks:
        fallback = MD_HEADING if is_adoc else ADOC_HEADING
        for i, line in enumerate(lines):
            m = fallback.match(line)
            if m:
                marks.append((i, len(m.group(1)), m.group(2).strip()))

    doc_name = os.path.basename(path)
    sections = []

    def emit(breadcrumb, start, end):
        body = "\n".join(lines[start:end]).strip()
        if not body:
            return
        if len(body) <= MAX_CHUNK_CHARS:
            sections.append((breadcrumb, start + 1, end, body))
            return
        # split long sections on blank lines, keeping line numbers honest
        buf, buf_start = [], start
        size = 0
        for idx in range(start, end):
            buf.append(lines[idx])
            size += len(lines[idx]) + 1
            if size >= MAX_CHUNK_CHARS and not lines[idx].strip():
                chunk = "\n".join(buf).strip()
                if chunk:
                    sections.append((breadcrumb, buf_start + 1, idx + 1, chunk))
                buf, buf_start, size = [], idx + 1, 0
        chunk = "\n".join(buf).strip()
        if chunk:
            sections.append((breadcrumb, buf_start + 1, end, chunk))

    if not marks:
        emit(doc_name, 0, len(lines))
        return sections

    # preamble before the first heading
    if marks[0][0] > 0:
        emit(doc_name + " (preamble)", 0, marks[0][0])

    stack = []  # (level, title)
    for n, (line_no, level, title) in enumerate(marks):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        breadcrumb = doc_name + " > " + " > ".join(t for _, t in stack)
        end = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
        emit(breadcrumb, line_no, end)

    return sections


# --------------------------------------------------------------------------
# index
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    title TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    path UNINDEXED,
    breadcrumb,
    body,
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
            raise SystemExit(
                "This Python's SQLite was built without FTS5. Try a different "
                "Python build, or install one via pyenv/conda."
            )
        raise
    return conn


def doc_title(text, path):
    for line in text.splitlines()[:40]:
        m = MD_HEADING.match(line) or ADOC_HEADING.match(line)
        if m:
            return m.group(2).strip()
    return os.path.basename(path)


def walk_docs(root, exts):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in exts:
                yield os.path.join(dirpath, name)


def reindex(conn, root, exts, force=False):
    """Incremental: content-hash each file, touch only what changed."""
    known = {r["path"]: r["digest"] for r in conn.execute("SELECT path, digest FROM files")}
    seen = set()
    added = changed = removed = 0

    for abspath in walk_docs(root, exts):
        rel = os.path.relpath(abspath, root)
        seen.add(rel)
        try:
            with open(abspath, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            log("skipping %s: %s" % (rel, e))
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if not force and known.get(rel) == digest:
            continue
        if rel in known:
            changed += 1
            conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
        else:
            added += 1
        rows = [(rel, bc, body, sl, el)
                for bc, sl, el, body in split_sections(text, abspath)]
        conn.executemany(
            "INSERT INTO chunks (path, breadcrumb, body, start_line, end_line) "
            "VALUES (?, ?, ?, ?, ?)", rows)
        conn.execute(
            "INSERT INTO files (path, digest, title) VALUES (?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET digest=excluded.digest, title=excluded.title",
            (rel, digest, doc_title(text, abspath)))

    for rel in set(known) - seen:
        removed += 1
        conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
        conn.execute("DELETE FROM files WHERE path = ?", (rel,))

    conn.commit()
    return {"added": added, "changed": changed, "removed": removed,
            "files": len(seen),
            "chunks": conn.execute("SELECT count(*) FROM chunks").fetchone()[0]}


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def build_match(query, joiner):
    tokens = TOKEN.findall(query)
    if not tokens:
        return None
    return (" %s " % joiner).join('"%s"' % t for t in tokens)


def search(conn, query, limit=5):
    """AND first for precision, fall back to OR so a long question still hits."""
    phrases = re.findall(r'"([^"]+)"', query)
    results = []
    attempts = []
    if phrases:
        attempts.append(" AND ".join('"%s"' % p for p in phrases))
    attempts.append(build_match(query, "AND"))
    attempts.append(build_match(query, "OR"))

    for match in attempts:
        if not match:
            continue
        try:
            rows = conn.execute(
                "SELECT path, breadcrumb, start_line, end_line, "
                "       snippet(chunks, 2, '**', '**', ' … ', 40) AS snip, "
                "       bm25(chunks, 0.0, 10.0, 1.0, 0.0, 0.0) AS score "
                "FROM chunks WHERE chunks MATCH ? ORDER BY score LIMIT ?",
                (match, limit)).fetchall()
        except sqlite3.OperationalError:
            continue
        if rows:
            results = rows
            break
    return results


def format_results(rows, query, root=None):
    """Render hits for an agent.

    Paths are ABSOLUTE. A relative path here is resolved by the caller's own
    file-reading tool against ITS working directory, which is not the docs
    root, so every follow-up read fails.

    The closing line deliberately does not name a tool. Telling the model to
    "read the file" next to a server called `docs` invites it to invent a
    `read_docs` tool on this server and then fail calling it.
    """
    if not rows:
        return ("No sections matched %r. Try fewer or different keywords, "
                "or call list_docs to see what is indexed." % query)
    out = ["%d section(s) for %r:" % (len(rows), query), ""]
    for r in rows:
        path = os.path.join(root, r["path"]) if root else r["path"]
        out.append("%s:%s-%s" % (path, r["start_line"], r["end_line"]))
        out.append("  %s" % r["breadcrumb"])
        out.append("  %s" % " ".join(r["snip"].split()))
        out.append("")
    out.append("These are absolute paths. For the full section, open one with your "
               "own file-reading tool at the line range shown. This server has no "
               "file-reading tool of its own.")
    return "\n".join(out)


def format_listing(conn, root=None):
    rows = conn.execute("SELECT path, title FROM files ORDER BY path").fetchall()
    if not rows:
        return "The index is empty. Call reindex_docs."
    lines = ["%d documents under %s:" % (len(rows), root or "the docs root"), ""]
    lines += ["%s — %s" % (r["path"], r["title"]) for r in rows]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# MCP over stdio (JSON-RPC 2.0, line delimited)
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "search_docs",
        "description": (
            "Full-text search over the local documentation knowledge base. "
            "Returns the best-matching sections as ABSOLUTE file path, line "
            "range, heading breadcrumb and a snippet. Prefer this over grepping "
            "or reading whole files: pass 3-6 distinctive keywords rather than a "
            "full sentence. Wrap a phrase in double quotes for an exact match. "
            "To read a result in full, use your OWN file-reading tool on the "
            "absolute path returned; this server does not read files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords to search for."},
                "limit": {"type": "integer", "description": "Max sections (default 5)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_docs",
        "description": ("List every indexed document as path plus title. Use when "
                        "you need a map of the knowledge base rather than an answer. "
                        "Takes no arguments."),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "reindex_docs",
        "description": ("Rescan the docs folder and refresh the index. Run after a "
                        "git pull; it only reprocesses files whose content changed. "
                        "Takes no arguments."),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]

TOOL_NAMES = [t["name"] for t in TOOLS]


def coerce_limit(value, default=5):
    """Models pass "5", 5.0 or "five". Never let that surface as a tool error."""
    if value in (None, ""):
        return default
    try:
        return max(1, min(20, int(float(value))))
    except (TypeError, ValueError):
        return default


def text_result(text):
    return {"content": [{"type": "text", "text": text}]}


def serve(conn, root, exts):
    log("litedocs-mcp %s serving %s" % (VERSION, root))
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            continue

        method, req_id = req.get("method"), req.get("id")
        params = req.get("params") or {}

        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "litedocs-mcp", "version": VERSION},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        # Clients probe these at startup even when the server declares no such
        # capability. Answering -32601 reads to the client as "this server is
        # broken" and can abort the session, so answer empty instead.
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        elif method == "prompts/list":
            result = {"prompts": []}
        elif method == "ping":
            result = {}
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments")
            if isinstance(args, str):           # some clients send a JSON string
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"query": args}
            if not isinstance(args, dict):
                args = {}
            try:
                if name == "search_docs":
                    query = str(args.get("query") or args.get("q") or "").strip()
                    if not query:
                        result = text_result(
                            "search_docs needs a 'query' string, e.g. "
                            '{"query": "retry policy"}. Available tools: %s.'
                            % ", ".join(TOOL_NAMES))
                    else:
                        result = text_result(format_results(
                            search(conn, query, coerce_limit(args.get("limit"))),
                            query, root))
                elif name == "list_docs":
                    result = text_result(format_listing(conn, root))
                elif name == "reindex_docs":
                    stats = reindex(conn, root, exts)
                    result = text_result(
                        "Reindexed: %(files)d files, %(chunks)d sections "
                        "(%(added)d new, %(changed)d updated, %(removed)d removed)."
                        % stats)
                else:
                    # Not flagged as an error: a hallucinated tool name should
                    # let the model self-correct, not count toward the client's
                    # repeated-failure cutoff.
                    result = text_result(
                        "This server has no tool named %r. It provides exactly: %s. "
                        "It cannot read files — use your own file-reading tool for "
                        "that." % (name, ", ".join(TOOL_NAMES)))
            except Exception as e:  # never kill the server on one bad call
                result = text_result("Tool error: %s" % e)
                result["isError"] = True
        elif method is not None and req_id is None:
            continue  # notification, nothing to answer
        else:
            if req_id is None:
                continue
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": "Method not found: %s" % method},
            }) + "\n")
            sys.stdout.flush()
            continue

        if req_id is not None:
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
            sys.stdout.flush()


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs", required=True, help="Folder of documentation to index.")
    ap.add_argument("--db", help="Index file (default: <docs>/.litedocs-index.sqlite3).")
    ap.add_argument("--exts", default=",".join(DEFAULT_EXTS),
                    help="Comma-separated extensions to index.")
    ap.add_argument("--index", action="store_true", help="Build the index and exit.")
    ap.add_argument("--force", action="store_true", help="Reindex every file.")
    ap.add_argument("--search", metavar="QUERY", help="Run one query and exit.")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    root = os.path.abspath(os.path.expanduser(args.docs))
    if not os.path.isdir(root):
        raise SystemExit("Not a directory: %s" % root)
    exts = {e if e.startswith(".") else "." + e
            for e in (x.strip().lower() for x in args.exts.split(",")) if e}
    db_path = args.db or os.path.join(root, ".litedocs-index.sqlite3")

    conn = connect(db_path)
    stats = reindex(conn, root, exts, force=args.force)

    if args.index:
        print("Indexed %(files)d files into %(chunks)d sections "
              "(%(added)d new, %(changed)d updated, %(removed)d removed)." % stats)
        print("Index: %s" % db_path)
        return
    if args.search:
        print(format_results(search(conn, args.search, args.limit), args.search, root))
        return

    log("indexed %(files)d files, %(chunks)d sections" % stats)
    serve(conn, root, exts)


if __name__ == "__main__":
    main()
