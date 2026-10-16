# litedocs-mcp

Search a folder of Markdown and AsciiDoc over MCP. SQLite FTS5, Python stdlib only, no embeddings, no Docker.

Point it at a documentation folder and your AI agent gets one precise `search_docs` tool instead of grepping its way through the repo. On a 220-file corpus it indexes in ~0.1s and answers queries in ~50ms.

```
agent  ──MCP/stdio──▶  litedocs-mcp  ──▶  SQLite FTS5 index  ──▶  your docs folder
```

---

## Why this exists

An agentic CLI pointed at a documentation repo will happily burn thousands of tokens on `grep`, then `read whole file`, then `grep` again. If your model access is metered — or the model is mediocre at long multi-step tool chains — that loop is expensive and unreliable.

litedocs-mcp replaces it with a single call that returns the five most relevant **sections**, each with a file path, a line range and a heading breadcrumb. The agent reads a full file only when it decides it needs to.

Most RAG servers solve this with embeddings, which means a vector database, a model download and often a Docker stack. For a few hundred text files that is the wrong weight class, and on a locked-down machine the model download is usually where the afternoon goes. litedocs-mcp uses SQLite's built-in FTS5 full-text index with BM25 ranking instead:

- **No dependencies.** Python standard library only. Nothing to `pip install`.
- **No network.** No model downloads, no API keys, no telemetry. Works fully offline.
- **No services.** One process, one SQLite file. Nothing to keep running between sessions.
- **Readable.** ~440 lines in a single file you can audit before pointing it at internal documentation.

On technical documentation — full of product names, service names, ticket IDs and domain jargon — keyword search is frequently *better* than naive embeddings, because those are exactly the tokens embedding models handle worst.

### When you want something else

Reach for an embedding-based server ([mcp-ragdocs](https://github.com/hannesrudolph/mcp-ragdocs), [mcp-server-qdrant](https://github.com/qdrant/mcp-server-qdrant)) when you need conceptual matching — finding the section about "rolling back a bad release" when the docs only ever say "reverting a deployment". litedocs-mcp will not find that; BM25 matches words, not meaning. The MCP interface is the same either way, so switching later costs you nothing.

---

## Requirements

- Python 3.9 or newer
- SQLite built with FTS5 (true for essentially every CPython build; the server tells you clearly if yours is the exception)

That's it.

---

## Install

### Option 1 — just the file

```bash
curl -O https://raw.githubusercontent.com/gowthamraj07/litedocs-mcp/master/litedocs_mcp.py
python3 litedocs_mcp.py --docs ./docs --index
```

### Option 2 — as a command

```bash
pip install git+https://github.com/gowthamraj07/litedocs-mcp.git
litedocs-mcp --docs ./docs --index
```

### Option 3 — no install at all

```bash
uvx --from git+https://github.com/gowthamraj07/litedocs-mcp.git litedocs-mcp --docs ./docs --index
```

---

## Quick start

Build the index and check that it found your documents:

```bash
$ python3 litedocs_mcp.py --docs ~/work/platform-docs --index
Indexed 214 files into 1893 sections (214 new, 0 updated, 0 removed).
Index: /home/you/work/platform-docs/.litedocs-index.sqlite3
```

Try a query from the shell before wiring in any agent:

```bash
$ python3 litedocs_mcp.py --docs ~/work/platform-docs --search "retry backoff dead letter"
1 section(s) for 'retry backoff dead letter':

guides/payments.md:5-15
  payments.md > Payment Service > Retry policy
  ## **Retry** policy Failed payment authorisations are **retried** three times
  with exponential **backoff** starting at 500ms. After the third failure the
  transaction moves to the **dead** **letter** queue for manual reconciliation. …

Read a file at the line range above for the full section.
```

If that looks right, run it as an MCP server:

```bash
python3 litedocs_mcp.py --docs ~/work/platform-docs
```

The server speaks MCP over stdio. It logs to stderr and keeps stdout clean for protocol traffic — if your client starts the server but shows no tools, **check stderr first**, that is the usual cause.

---

## Connecting an agent

Use **absolute paths** for both the script and the docs folder. Your client launches the server from an unpredictable working directory.

### Claude Code (and its forks)

```json
{
  "mcpServers": {
    "docs": {
      "command": "python3",
      "args": ["/abs/path/to/litedocs_mcp.py", "--docs", "/abs/path/to/your/docs"]
    }
  }
}
```

Put this in `.mcp.json` at your project root (shared with the team) or in your user-level Claude Code config.

### Claude Desktop

Same block, in `claude_desktop_config.json`:

- macOS — `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows — `%APPDATA%\Claude\claude_desktop_config.json`
- Linux — `~/.config/Claude/claude_desktop_config.json`

### OpenCode

```bash
opencode mcp add
```

…or add it to `opencode.json` / `~/.config/opencode/opencode.jsonc` as a local command.

### Anything else

Any MCP client that can launch a local stdio subprocess works. Point it at `python3 /abs/path/litedocs_mcp.py --docs /abs/path/docs`.

---

## The tools

Deliberately three. Tool descriptions consume context on every single turn, so a server with a dozen tools is working against the thing it is supposed to help with.

### `search_docs(query, limit=5)`

Full-text search. Returns the best-matching sections as path, line range, heading breadcrumb and a snippet with matched terms marked.

Pass **3–6 distinctive keywords**, not a whole sentence. The tool description tells the model this, because weaker models habitually dump the user's entire question into search tools, which wrecks BM25 ranking.

Wrap a phrase in double quotes for an exact match:

```
search_docs(query='"Idempotency-Key" header merchant')
```

### `list_docs()`

Every indexed document as path plus title. Use it when you want a map of the knowledge base rather than an answer — and run it once after your first index to spot files that came back suspiciously thin.

### `reindex_docs()`

Rescan and refresh. Only files whose content actually changed get reprocessed, so this is cheap enough to run after every `git pull`.

The server also reindexes automatically on startup, so in practice you rarely need this one.

---

## CLI reference

```
--docs PATH       Folder of documentation to index. Required.
--db PATH         Index file. Default: <docs>/.litedocs-index.sqlite3
--exts LIST       Comma-separated extensions.
                  Default: .md,.markdown,.adoc,.asciidoc,.txt
--index           Build the index and exit.
--force           Reindex every file, ignoring content hashes.
--search QUERY    Run one query and exit. Useful for tuning without an agent.
--limit N         Max results for --search. Default: 5.
```

With no `--index` and no `--search`, it runs as an MCP server on stdio.

---

## How it works

**Chunking.** Documents are split at heading boundaries — `#` for Markdown, `=` for AsciiDoc — with a heading stack tracked so every chunk carries a breadcrumb like `runbook.adoc > Incident Runbook > Escalation path`. Headings inside fenced code blocks are ignored. Content before the first heading becomes a preamble chunk. Sections longer than 2000 characters are split at paragraph boundaries, with line numbers kept honest throughout. If a file uses the other syntax's heading style, the parser falls back to it rather than producing one giant chunk.

**Indexing.** Chunks go into a standalone FTS5 virtual table with the Porter stemmer, so "retry" matches "retried". The heading breadcrumb is weighted 10× the body in BM25 scoring — a section whose heading is about your query is almost always the one you want.

**Incremental updates.** Each file's content is SHA-256 hashed. On reindex, unchanged files are skipped, changed files have their chunks deleted and rebuilt, and deleted files are purged. A no-op reindex over 220 files takes ~50ms.

**Query handling.** The query is tokenised and tried as an AND match first, for precision. If that returns nothing it falls back to OR, so a long natural-language question still finds something instead of silently returning empty. Quoted phrases are extracted and tried first. Malformed FTS5 syntax can never crash the server.

---

## Known limitations

**AsciiDoc `include::` is not resolved.** Included files are indexed on their own, so no content is lost — but a master document that is mostly `include::` directives will index as near-empty. Run `list_docs` after your first index to spot these.

**AsciiDoc attribute substitution is not performed.** If your docs use `{product-name}`, searching for the resolved value will not hit.

**No conceptual matching.** See [When you want something else](#when-you-want-something-else) above.

**The index file lives next to your docs** by default, as `.litedocs-index.sqlite3`. Add it to `.gitignore` (this repo's own `.gitignore` covers it) or relocate it with `--db`.

---

## Privacy

litedocs-mcp makes no network calls of any kind. It reads local files and writes one local SQLite file. There are no API keys, no model downloads, no telemetry, and no third-party dependencies that could add any of the above in a future version.

That is a deliberate design goal, not an accident — the intended use case is documentation that cannot leave the machine.

---

## Searching history: `litedocs-history`

A second, separate server for a different question: **what did the docs used to say?**

If your documentation lives in git — a GitHub wiki is an ordinary git repo at `https://github.com/OWNER/REPO.wiki.git` — then every version of every page is still there. `litedocs-history` indexes all of them, so you can search text that has since been deleted and find out exactly when it was live and who changed it.

It is a separate entry point with its own index file, on purpose. Tool descriptions cost context on every turn, and history search is something you need rarely. Enable it when you are auditing; leave it off the rest of the time.

```bash
git clone https://github.com/OWNER/REPO.wiki.git ~/work/project.wiki
litedocs-history --repo ~/work/project.wiki --index
```

```
$ litedocs-history --repo ~/work/project.wiki --search "three business days settle"
1 match(es) for 'three business days settle' across the full history:

Payments.md — Payments.md > Payments > Settlement
  added  2026-01-15 by alice (efc8779e)
  status REMOVED 2026-05-20 by carol (17fa2521)
  ## Settlement Funds **settle** within **three** **business** **days** of authorisation.
```

That text exists nowhere in the working tree. Ordinary grep cannot find it.

### Tools

| Tool | Answers |
|---|---|
| `search_history(query, limit, only_removed)` | "Did the docs ever say something like this, and when?" Fuzzy, ranked, finds deleted text. |
| `when_documented(text, path)` | "Which commit added this exact sentence, and which removed it?" Git pickaxe, literal match, author and date. |
| `page_history(path, limit)` | "How has this page changed?" Follows renames. |

The intended flow is two-stage: `search_history` to *find* the wording, `when_documented` to *prove* the provenance.

### CLI

```
--repo PATH        Clone of the docs/wiki git repo. Required.
--db PATH          Index file. Default: <repo>/.litedocs-history.sqlite3
--index            Build the index and exit (skipped if refs are unchanged).
--force            Rebuild even if nothing has changed.
--search QUERY     Search all history.
--only-removed     Restrict to text that no longer exists.
--when TEXT        Pickaxe a literal phrase. Needs no index.
--page PATH        Revisions of one page. Needs no index.
```

### How it stays cheap

Indexing every version of every page sounds expensive. It isn't, because of two things.

Git stores one blob per distinct content, so a page edited fifty times contributes fifty blobs, not fifty copies of the repo. All of them are read in a single `git cat-file --batch` call.

More importantly, **section lifetimes are collapsed**. A section that survives twenty edits to other parts of its page becomes *one* row spanning the whole period it was live — not twenty near-identical search hits. So the index is sized by how much the docs actually changed, and results read as "this was true from January to May" rather than as a wall of duplicates.

### Limits worth knowing

**Git history is not tamper-evident.** GitHub wikis, as far as I know, support neither branch protection nor required reviews, so anyone with write access can force-push. This tool reports faithfully what the history contains; it cannot tell you the history was never rewritten. If the audit trail genuinely matters, mirror the wiki on a schedule into a repo you control.

**The repo is not a record of what anyone read.** A commit date establishes what the docs said on a given day. It does not establish that a particular person saw it. Pair it with a ticket or a decision record for that.

**Merge commits are skipped**, as `git log --raw` does by default. Wiki histories are linear in practice; a heavily merged docs repo may miss changes that only exist in a merge resolution.

**`when_documented` is a literal match.** It is git's pickaxe — character for character, no stemming. Use `search_history` first to find the real wording.

---

## Contributing

Issues and pull requests welcome. Two things worth preserving if you send a patch: zero runtime dependencies, and a single auditable file. Both are features.

---

## License

MIT — see [LICENSE](LICENSE).
