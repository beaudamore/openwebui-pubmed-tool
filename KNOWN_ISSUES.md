# Known Issues — PubMed Tools

Audit date: **2026-08-26**. Covers the two deployed Open WebUI tools backed by this repo:

| deployed artifact | source file | blocking sites |
|---|---|---|
| `pubmed_deep_research_tool_v3` | `tool/pubmed_internal_v3.py` | 7 |
| `pubmed_internal_2` | `tool/pubmed_internal_v2.py` | 6 |

Both are **tools**, so they execute during a live chat turn. See `../../AUDIT-2026-08-26.md` for the
stack-wide picture.

---

## 1. Blocking network I/O inside async code (HIGH — these are tools)

Open WebUI moved plugin execution to async. Synchronous `requests` calls made from inside an `async def`
**block the entire Open WebUI event loop** — freezing every chat, user, and automation on the instance for
the duration. Neither file contains `run_in_executor` or `asyncio.to_thread`.

This is worse here than in the ingest pipes: a tool runs while a user is waiting on a response, at whatever
time they happen to ask, rather than at 09:00 when nobody is around.

### `pubmed_deep_research_tool_v3` — 7 sites, all in `deep_research_pubmed()`

- line 300 — `requests.get()`
- line 320 — `requests.get()`
- line 349 — `requests.get()`
- line 360 — `fetch_pmc_figures()` *(sync helper)*
- line 400 — `pubmed_search()` *(sync helper)*
- line 435 — `get_pubmed_spell_suggestion()` *(sync helper)*
- line 832 — `download_pmc_figures()` *(sync helper — OA tarball, `timeout=120`)*

### `pubmed_internal_2` — 6 sites, all in `deep_research_pubmed()`

- line 298 — `requests.get()`
- line 318 — `requests.get()`
- line 347 — `requests.get()`
- line 358 — `fetch_pmc_figures()` *(sync helper)*
- line 398 — `pubmed_search()` *(sync helper)*
- line 433 — `get_pubmed_spell_suggestion()` *(sync helper)*

The two share the same `deep_research_pubmed()` shape with a ~2-line offset — **fix once, apply twice.**
The v3 tarball download at `timeout=120` is the worst single case: one article can stall the instance for
two minutes.

Note that only 3 of 7 (and 3 of 6) are direct `requests` calls. The rest reach the network through
synchronous helpers, so **grep will not find them** — use an AST pass that follows one hop into sync
helpers.

**Fix:** wrap each in `await asyncio.to_thread(...)`, or move to `httpx.AsyncClient`. Keep the existing
explicit timeouts, which are already correct.

## 2. No retry or backoff (MEDIUM)

No `time.sleep`, `backoff`, `max_retries`, or retry loop in either file. NCBI E-utilities rate-limits
(10 req/sec with an API key, 3 without) and returns HTTP 429. Any throttle or transient failure silently
drops that article, and the tool reports success regardless.

**Fix:** exponential backoff honouring `Retry-After` on 429; report retry/failure counts in the response so
partial results are visible to the user.

---

## Deployed vs repo

`pubmed_deep_research_tool_v3` is **196 AST nodes** ahead in Open WebUI relative to
`tool/pubmed_internal_v3.py`; `pubmed_internal_2` is **identical in logic** to `tool/pubmed_internal_v2.py`.
Textual diffs look far larger than that because local files are **CRLF** while Open WebUI stores **LF**, and
the deployed copies are Black-formatted. Always compare by AST — see `../../AUDIT-2026-08-26.md` §6.

`Pubmed Knowledge Base` (`1e399949`, 25 files) was created 2026-08-09 15:07, the same minute
`pubmed_deep_research_tool_v3` was last updated — that is the tool-era KB, distinct from the pipe's.
