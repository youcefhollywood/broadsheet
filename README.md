# Broadsheet

**A living daily newspaper that learns what you want to read.**

Broadsheet is a memory-driven news agent. Twice a day it pulls from 23 international
RSS feeds, uses Qwen (`qwen3.6-flash`) to synthesise a front page, and quietly studies how you react to
each story. Over time it builds a model of your taste, reshapes future editions around
it, and, when it has noticed a real pattern, asks you a single sharp question to confirm
its inference before acting on it.

It is built for the Qwen hackathon (Track 1: MemoryAgent) and runs live on Alibaba Cloud
Function Compute, persisting its memory to Alibaba Cloud OSS.

---

## What makes it a MemoryAgent

The point of Broadsheet is not that it shows the news. It is that it *remembers* and
*reasons*. Three kinds of memory accumulate across editions and persist in object storage:

- **Reaction log** — append-only ground truth of every reaction you have made (love /
  less / never), with the tags of the story you reacted to.
- **Reader model** — the agent's own prose theory of what you like, re-derived from the
  reaction log. It carries hypotheses *and* the tests the agent wants to run to confirm
  them in the next edition.
- **World state** — a carried-forward distillation of what is ongoing across editions, so
  the front page can note what has *developed* rather than repeating itself.

Because this memory builds over days, the agent's behaviour visibly sharpens. A first
question might be broad ("should I deprioritise dry policy stories?"); a later one,
after more reactions, is specific and confident ("you have passed on every story
involving courts, police, or political negotiations, so I am shifting toward human
resilience and concrete tech/science breakthroughs — is that right?").

---

## Architecture

```
   RSS feeds (23, international)
            │
            ▼
   ┌───────────────────────────────────────────────┐
   │   Alibaba Cloud Function Compute (web fn)      │
   │                                                │
   │   Flask app (app.py)  ──►  orchestrator        │
   │        routes:               │                 │
   │        /          page       ├─ sources        │
   │        /produce   build       ├─ synthesis ─┐   │
   │        /ingest    react       ├─ preference │   │ Qwen
   │        /invoke    timer        ├─ question   ├──►│ (qwen3.6-flash
   │        /edition   json         └─ world_state┘   │  DashScope intl)
   │        /health                                 │
   └───────────────────────────────────────────────┘
            │                         ▲
            │ state via OSS REST      │ twice-daily Time Trigger
            ▼                         │ (06:00 & 18:00 UTC → POST /invoke)
   ┌─────────────────────────┐
   │  Alibaba Cloud OSS       │   state/broadsheet_state.json
   │  (object storage)        │   = reaction log + reader model
   │                          │     + world state + editions
   └─────────────────────────┘
```

See `docs/architecture.svg` for a rendered diagram.

### Why these choices

- **Custom runtime, Python 3.10, built-in `wsgiref` server.** The custom runtime does not
  auto-install `requirements.txt`, so dependencies are installed into `/code` inside the
  function. `gunicorn` was dropped in favour of Python's standard-library WSGI server to
  keep the runtime dependency-light.
- **OSS via REST + `requests`, not the `oss2` SDK.** `oss2` pulls in a vendored
  `pyOpenSSL` that clashes with the function base image, so `state_store.py` signs OSS
  requests manually (V1 signature) and talks to OSS over plain HTTPS. Temporary
  credentials are read from the function's injected role environment variables.
- **Reasoning is batched at produce time, not per reaction.** A reaction is a cheap, local
  write that sets a `pending_reasoning` flag. The next `produce` re-derives the reader
  model and world state once, over all accumulated reactions. Reactions therefore feel
  instant, and Qwen is called far less often.
- **Per-article categorisation.** The synthesis model assigns each story's true topic
  (tech, business, world, …) from its content, rather than inheriting the source feed's
  tag — so a tech story from a general world outlet is labelled `tech`.

---

## Repository layout

| File | Role |
|------|------|
| `app.py` | Flask web app: routes, the newspaper HTML/CSS, reaction UI |
| `orchestrator.py` | Ties the pipeline together: `produce_edition`, `ingest_reactions` |
| `sources.py` | The 23 RSS feeds and the fetch logic |
| `synthesis.py` | Qwen prompt + parse for the front page and per-article categories |
| `preference.py` | Reaction log + reader-model reasoning |
| `question.py` | Decides whether the agent has earned a question to ask |
| `world_state.py` | Carried-forward distillation of ongoing stories |
| `state_store.py` | The only module that touches storage: OSS via REST + `requests` |
| `sample_articles.py` | Offline sample feed for local testing without network |
| `bootstrap` | Function Compute entry script (`exec python3 app.py`) |
| `requirements.txt` | `flask`, `feedparser`, `dashscope`, `requests` |

---

## Running locally

```bash
pip install -r requirements.txt
export DASHSCOPE_API_KEY=sk-...           # Qwen key (DashScope, international endpoint)
export OSS_BUCKET=your-bucket-name        # optional locally; required on the function
export OSS_ENDPOINT=oss-<region>.aliyuncs.com   # OSS endpoint host
python3 app.py                            # serves on :9000
# then in another shell:
curl -X POST localhost:9000/produce       # build an edition (uses live RSS + Qwen)
open http://localhost:9000/               # read it
```

Local runs use real RSS and Qwen but write state to OSS only when OSS credentials are
present in the environment; without them, `state_store.py` degrades to in-memory state.

## Deploying to Alibaba Cloud (outline)

1. **OSS bucket** (Standard, private) to hold `state/broadsheet_state.json`.
2. **RAM role** trusted by Function Compute, with OSS access, attached to the function so
   temporary credentials are injected as environment variables.
3. **Function Compute web function** (custom runtime, Python 3.10, port 9000), with
   `DASHSCOPE_API_KEY` set and the role attached. Also set `OSS_BUCKET` and
   `OSS_ENDPOINT` (e.g. `oss-<region>-internal.aliyuncs.com`) so the app knows
   where to persist state.
4. Install dependencies into `/code` inside the function, mark `bootstrap` executable,
   and deploy.
5. **HTTP trigger** (anonymous) for the browser, and a **Time Trigger** firing
   `0 0 6,18 * * *` (06:00 / 18:00 UTC) which posts to `/invoke` to produce an edition.

---

## Tech

Qwen (`qwen3.6-flash`) via the DashScope SDK, international endpoint. Alibaba Cloud
Function Compute and OSS. Python 3.10, Flask, feedparser.

## License

MIT — see [LICENSE](LICENSE).
