# Takshashila Knowledge Assistant

A retrieval-augmented assistant over Takshashila Institution's knowledge:

* the **public website** (takshashila.org.in) — publications (+ PDFs), blogs, op-eds
  listed under "In the news", team profiles, research areas, courses, programmes,
  events, books, trackers, podcasts;
* the **authenticated Commit Knowledge Base** (internal decisions, playbook, insights);
* curated internal files (e.g. the 2026 holiday list).

Answers are generated only from retrieved passages, every citation is verified against
the passage it cites, and insufficient evidence yields an explicit refusal.

| Interface | Where | Scope |
|---|---|---|
| Web UI | GitHub Pages (`frontend/`) | public website content; staff token unlocks internal |
| REST API | Render Free (`api/main.py`, Docker) | same |
| Mattermost `/askkb` | same Render service (`/mattermost/*`) | internal (Commit KB + website) |
| Streamlit dashboard | local / internal (`app.py`) | all; admin tabs password-gated |

**Production (no laptop involved):**
**GitHub Pages** = frontend · **Render Free** = FastAPI backend (`https://<service>.onrender.com`) ·
**GitHub Actions** = CI, Pages deployment, daily KB refresh + encrypted bundle ·
**Mattermost** `/askkb` → Render `/mattermost/ask`. Setup: [DEPLOYMENT](DEPLOYMENT.md).

The knowledge base refreshes itself **every day at 06:00 Asia/Kolkata** on GitHub
Actions — incrementally, validated, and promoted atomically — with no laptop involved.

**Docs:** [ARCHITECTURE](ARCHITECTURE.md) · [DEPLOYMENT](DEPLOYMENT.md) ·
[CRAWLER](CRAWLER.md) · [OPERATIONS](OPERATIONS.md) · [TESTING](TESTING.md) ·
[TROUBLESHOOTING](TROUBLESHOOTING.md) · [Mattermost bot](README_MATTERMOST_BOT.md) ·
reports: [AUDIT_REPORT](AUDIT_REPORT.md), [IMPLEMENTATION_REPORT](IMPLEMENTATION_REPORT.md),
[CRAWL_AUDIT](CRAWL_AUDIT.md), [TEST_REPORT](TEST_REPORT.md)

---

## Quick start (local development)

```bash
python -m venv .venv && . .venv/Scripts/activate          # Windows; use bin/activate elsewhere
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt -r requirements-dev.txt
cp env.example .env                                          # fill in GROQ_API_KEY, Commit KB creds…

python scripts/refresh_kb.py            # crawl + build + validate + promote (first run: full build)
uvicorn api.main:app --port 8000        # API + Mattermost routes
streamlit run app.py                    # dashboard
API_BASE_URL=http://localhost:8000 python frontend/build.py && python -m http.server -d frontend/dist 5500
```

Ask the API:

```bash
curl -s -X POST localhost:8000/api/query -H 'Content-Type: application/json' \
     -d '{"query":"What has Takshashila published about geospatial technology?","mode":"normal"}'
```

Response: `{answer, citations[{n,title,url,source,access,content_type_label,authors,date,publisher,excerpt}], sources, confidence, grounded, metadata{request_id, latency_seconds, scope, kb_version, timings, grounding}}`.

## Knowledge-base commands

```bash
python scripts/refresh_kb.py [--website|--commit-kb|--all] [--full] [--dry-run]
python scripts/refresh_kb.py --validate-only | --rebuild-only | --audit-only | --status
python scripts/audit_website.py --from-last-crawl [--check-external]
python scripts/validate_kb.py --json
```

Every refresh builds a **new release** under `data/releases/<version>/`, validates it
(including regression guards against the previous release) and smoke-tests real
retrieval before a single atomic pointer switch (`data/releases/CURRENT`). A failed
crawl or failed validation never replaces the working KB.

## Repository layout

```
api/               production FastAPI app (+ Pydantic schemas)
integrations/      Mattermost bot (router, formatting, routing, signed callbacks)
src/               the engine: config, crawl state, extraction, metadata, chunking,
                   embeddings (+cache), FAISS/BM25 state, retriever, RAG pipeline,
                   citations, people graph, refresh orchestration, bundle/sync
scripts/           crawl engine, refresh CLI + gate, validation, audit, release tools
frontend/          GitHub Pages site (HTML/CSS/JS; build.py injects the API URL)
tests/             hermetic pytest suite (fake embeddings; no network, no LLM)
.github/workflows/ ci.yml, deploy-pages.yml, kb-refresh.yml
Dockerfile, render.yaml  Render Free deployment (see DEPLOYMENT.md)
app.py             Streamlit dashboard
```

## Configuration

All configuration is environment-driven; see `env.example` for every variable.
Secrets (Groq key, Commit KB credentials, Mattermost tokens, bundle key, staff tokens)
live only in `.env` locally, Render environment variables and GitHub secrets — never in the repo
or the frontend.

## Tests

```bash
pytest -q                                  # 170+ hermetic tests (~10 s)
ruff check .
RUN_INTEGRATION=1 pytest tests/test_integration_real_index.py   # against the real index
```
