# OpenWebUI Headless Tool Triggering (No-Chat-Persisted API Calls)

> **Auxiliary technique:** This behavior belongs to Open WebUI's API and is not implemented by the Circle of Speakers `pipeline-public` code.

How to trigger an OpenWebUI model's bound tools (e.g. a PubMed research tool) on a
recurring schedule from an **external script**, without using OpenWebUI's built-in
Automations feature and without leaving a visible chat behind every run.

## The Problem

OpenWebUI's **Automations** feature (`User Menu → Automations`, RRULE-scheduled
prompts) always creates a brand-new, permanently-persisted chat every time it fires.
Confirmed directly in the backend source
(`backend/open_webui/utils/automations.py`, `execute_automation()`):

```python
chat_id = str(uuid4())
chat = await Chats.insert_new_chat(chat_id, automation.user_id, ...)
```

There is no config flag to reuse a chat, suppress persistence, or auto-archive the
result. An hourly automation means an hourly new entry in the chat sidebar, forever.
`AutomationData` only stores `prompt`, `model_id`, `rrule`, and an optional
`terminal` config — nothing controls chat lifecycle.

This is fine for a human-readable weekly digest, but wrong for a pure
"keep a knowledge base fresh" job that nobody needs to read as a chat transcript.

## The Fix: `local:` chat_id prefix

The real `/api/chat/completions` endpoint (the same endpoint the frontend calls for
every message) has a documented escape hatch. From
`backend/open_webui/main.py`, inside `chat_completion()`:

```python
if not chat_id.startswith('local:') and not chat_id.startswith('channel:'):
    # temporary/channel chats are not stored
    ...
```

If the `chat_id` you send is prefixed `local:`, OpenWebUI runs the **full**
chat-completion pipeline — filters, RAG, model params, and any bound tools — but
**never writes a chat row to the database** and never emits the `chat:list` socket
event that would make it appear in the sidebar. This is the same mechanism behind
the frontend's "Temporary Chat" mode.

This means: call the model directly via API, on whatever schedule you want, and get
tool execution (e.g. a PubMed KB-fill) with zero chat clutter — no need to write a
second copy of the tool's fetch/parse logic, and no need to fight the Automations
scheduler's chat-persistence behavior.

## Prerequisites

1. **Bind the tool to a model preset first.** Workspace → Models → create/clone a
   preset, attach the desired tool (e.g. `deep_research_pubmed`) as a default tool.
   Note the model's id.
2. **Generate an API key.** Settings → Account → API Keys.
3. **Look up the tool's id.** `GET /api/v1/tools/` on your instance — the backend
   does **not** auto-resolve a model's default `tool_ids` for direct API calls the
   way it does for the built-in Automations executor
   (`_resolve_model_tool_ids()` in `automations.py` is only called from
   `execute_automation()`). You must pass `tool_ids` explicitly.

## Request

```python
import requests
import uuid

API_KEY = "sk-..."
BASE_URL = "https://your-owui-host"
MODEL_ID = "oncology-pubmed-digest"   # model preset with the tool bound
PUBMED_TOOL_ID = "pubmed_deep_research"  # from GET /api/v1/tools/

def run_headless_prompt(topic: str) -> dict:
    resp = requests.post(
        f"{BASE_URL}/api/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": MODEL_ID,
            "chat_id": f"local:{uuid.uuid4()}",  # never persisted — the whole trick
            "parent_id": None,
            "stream": False,
            "tool_ids": [PUBMED_TOOL_ID],
            "messages": [
                {"role": "user", "content": f"Search PubMed for: {topic}"}
            ],
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()
```

Run this from cron / systemd timer, hourly, once per topic (or loop over a topic
list per invocation).

## What you get vs. what you don't

| | Automations (RRULE) | `local:` headless call |
|---|---|---|
| Persisted, readable chat | Yes, every run | No, never |
| Sidebar clutter on high frequency | Yes | No |
| Tool auto-resolved from model defaults | Yes (`_resolve_model_tool_ids`) | No — pass `tool_ids` explicitly |
| Filters / RAG / model params still run | Yes | Yes |
| Good for | Human-readable digests (e.g. weekly oncology literature summary) | Silent background jobs (e.g. hourly KB-fill) |

## Dedup note

The PubMed tool's own PMID-based deduplication logic (tracked inside its knowledge
base handling) still runs normally on every call, since this is the real tool
executing through the real pipeline — not a reimplementation. No extra dedup logic
is needed on the calling script's side.
