# Deployment — laptop-independent production (free tier)

| Piece | Where | What it does |
|---|---|---|
| **Frontend** | **GitHub Pages** (`.github/workflows/deploy-pages.yml`) | Static UI → `POST https://<service>.onrender.com/api/query` |
| **Backend** | **Render Free Web Service** (Docker, `render.yaml`) | FastAPI: `/api/*`, `/health`, `/ready`, `/rag/status`, RAG engine, FAISS + BM25, Groq |
| **Mattermost** | **Render** `/mattermost/ask` (same service) | `/askkb` slash command on the same RAG engine and security checks |
| **KB refresh** | **GitHub Actions** (`.github/workflows/kb-refresh.yml`) | 06:00 Asia/Kolkata: crawl website + Commit KB → build → validate → smoke test → publish **encrypted** bundle to the `kb-latest` release |

```
GitHub Pages ──HTTPS /api/query──▶ Render Free (FastAPI + RAG + FAISS/BM25 + Groq, /mattermost/ask)
                                          ▲  downloads + verifies + decrypts
GitHub Actions 06:00 IST ──publishes──▶ kb-latest release (encrypted bundle + manifest)
```

The API never crawls. It downloads the latest encrypted, validated KB bundle that
GitHub Actions publishes, on startup and every `KB_SYNC_INTERVAL_MINUTES`.

Repository: `GopalTomar/Takshashila-Knowledge-Assistant-Chatbot` (a *project* Pages
repository). Public site: **https://gopaltomar.github.io/Takshashila-Knowledge-Assistant-Chatbot/**
(the frontend uses only relative asset paths, so it works under that sub-path).

### Render Free constraints and how they are handled

| Constraint | Handling |
|---|---|
| **512 MB RAM, 0.1 CPU** | The image runs query embeddings on the ONNX export of the same model (no torch; parity verified at build — the build fails if vectors differ), BM25 is stored as compact arrays (scores identical to rank_bm25), metadata is streamed and de-duplicated. Measured resident memory is in `TEST_REPORT.md`. |
| **Ephemeral disk** | Nothing permanent is stored on Render. Every start downloads the latest bundle from the `kb-latest` release, verifies size + SHA-256, decrypts (AES-256-GCM, authenticated) and loads it. |
| **Sleeps after ~15 min idle** | A wake-up takes a few minutes (download + load at 0.1 CPU). `/ready` is 503 meanwhile; the web UI shows "Service starting…"; `/askkb` answers "starting up, ask again in two minutes". Optional: `.github/workflows/keepalive.yml` pings `/health` every 10 min (set variable `RENDER_KEEPALIVE_URL`); one always-on service fits Render's 750 free hours/month. |
| **Daily new KB** | `KB_LOW_MEMORY=true`: the new release is downloaded, verified and unpacked on disk first; then the old in-memory KB is released and the new one loaded (~1 min of 503 once a day). If loading fails, the previous release is loaded back. A missing/corrupt bundle never replaces the working KB. |

### Where every secret lives

| Place | Values |
|---|---|
| **Local only** (`.env`, never committed) | anything, for development |
| **Render** environment (secrets) | `GROQ_API_KEY`, `KB_BUNDLE_KEY`, `API_ACCESS_TOKENS`, `MATTERMOST_BOT_TOKEN`, `MATTERMOST_SLASH_TOKEN`, `MATTERMOST_ACTION_SECRET` (+ `MATTERMOST_BOT_PUBLIC_URL`) |
| **GitHub Actions secrets** | `KB_BUNDLE_KEY`, `COMMIT_KB_USERNAME`, `COMMIT_KB_PASSWORD` |
| **Public** (GitHub Actions *variable*) | `API_BASE_URL=https://<service>.onrender.com` |

The Commit KB credentials are **not** needed on Render (the API never crawls — it
downloads the encrypted bundle), and the refresh needs neither `GROQ_API_KEY`
(its smoke test is retrieval-only) nor `HF_TOKEN` (the embedding model is public).
Nothing secret is ever written to `frontend/`, the Pages site, or the image.

---

## 0. One-time preparation (local)

```bash
python -m src.kb_bundle genkey                               # KB_BUNDLE_KEY (GitHub secret + Render)
python -c "import secrets; print(secrets.token_urlsafe(32))" # staff tokens → API_ACCESS_TOKENS
python -c "import secrets; print(secrets.token_urlsafe(32))" # MATTERMOST_ACTION_SECRET
```

## 1. GitHub repository settings

**Settings → Secrets and variables → Actions → Secrets**

| Secret | Value |
|---|---|
| `KB_BUNDLE_KEY` | output of `genkey` (identical value on Render) |
| `COMMIT_KB_USERNAME` | Commit KB basic-auth username |
| `COMMIT_KB_PASSWORD` | Commit KB basic-auth password |

**Variables**

| Variable | Value |
|---|---|
| `API_BASE_URL` | `https://<service>.onrender.com` (after step 3) — **required** for the Pages build |
| `RENDER_KEEPALIVE_URL` | `https://<service>.onrender.com` — optional, keeps the API awake |
| `KB_REFRESH_TIME` / `KB_REFRESH_TIMEZONE` / `KB_REFRESH_ENABLED` | optional (defaults `06:00` / `Asia/Kolkata` / `true`) |
| `COMMIT_KB_URL` | optional (default `https://commit.takshashila.org.in/`) |

**Settings → Pages → Build and deployment → Source: GitHub Actions.**
Workflow permissions can stay "Read" — each workflow declares the write
permissions it needs (`contents: write` for release uploads, `pages: write`).

## 2. First knowledge-base release

Either let Actions bootstrap it (full crawl on GitHub's servers, ~30–60 min):

```bash
gh workflow run kb-refresh.yml -f force=true -f full=true && gh run watch
```

or publish a release built locally (the key must equal the `KB_BUNDLE_KEY` secret):

```bash
export KB_BUNDLE_KEY=...
python scripts/kb_release.py pack --out-dir dist-kb
gh release create kb-latest --prerelease --title "Knowledge base (latest, encrypted)" --notes "Encrypted KB bundle"
gh release upload kb-latest dist-kb/*.tkkb dist-kb/kb-manifest.json --clobber
```

Check: `gh release view kb-latest` lists `kb-manifest.json` and `kb-<version>.tkkb`.
The bundle is encrypted, so publishing it in a public repository exposes nothing
without `KB_BUNDLE_KEY`.

## 3. Render (backend + Mattermost)

**Render Dashboard → New → Blueprint → connect GitHub → select this repository →
Apply.** `render.yaml` creates the service; Render asks for every `sync: false`
value (secrets are stored in Render, never in Git).

### Render deployment checklist

| Item | Value |
|---|---|
| Service type | Web Service, runtime **Docker**, plan **Free** |
| Repository / branch | `GopalTomar/Takshashila-Knowledge-Assistant-Chatbot` / `main` |
| Build | `Dockerfile` (two stages: ONNX export with parity check → torch-free runtime); no build command |
| Start command | image default: `uvicorn api.main:app --host 0.0.0.0 --port $PORT` (Render injects `PORT`; never set it) |
| Health check path | `/health` (liveness). Readiness: `/ready` → 200 once the KB is loaded |
| Auto-deploy | on every commit to `main` (`autoDeployTrigger: commit`) |
| HTTPS | automatic: `https://<service>.onrender.com` |
| CORS | `CORS_ALLOW_ORIGINS=https://gopaltomar.github.io` (origin only) |
| API_BASE_URL | set the GitHub **variable** to `https://<service>.onrender.com` |
| Mattermost URL | slash command → `https://<service>.onrender.com/mattermost/ask` |

### Environment variables (Render → service → Environment)

Secrets (enter values in Render; never commit):

| Variable | Value |
|---|---|
| `GROQ_API_KEY` | Groq API key |
| `KB_BUNDLE_KEY` | same as the GitHub secret |
| `API_ACCESS_TOKENS` | comma-separated staff tokens (step 0) |
| `MATTERMOST_BOT_TOKEN` | bot account token |
| `MATTERMOST_SLASH_TOKEN` | slash command token (step 5) |
| `MATTERMOST_ACTION_SECRET` | random string (signs buttons/dialogs) |
| `MATTERMOST_BOT_PUBLIC_URL` | `https://<service>.onrender.com` |

Pre-set by `render.yaml` (non-secret): `KB_BUNDLE_MANIFEST_URL`,
`KB_SYNC_INTERVAL_MINUTES=30`, `KB_LOW_MEMORY=true`,
`CORS_ALLOW_ORIGINS=https://gopaltomar.github.io`, `PUBLIC_SOURCES=website`,
`GROQ_MODEL=openai/gpt-oss-120b`, `MATTERMOST_URL`, `MATTERMOST_BLOCK_GUESTS=true`,
`MATTERMOST_WARM_RAG_ON_STARTUP=false`, `LOG_QUERY_TEXT=false`.
Set by the image: `EMBEDDING_BACKEND=onnx`, `EMBEDDING_ONNX_DIR`, `DATA_DIR=/app/data`.

After the first deploy, note the URL and set `MATTERMOST_BOT_PUBLIC_URL` to it.

## 4. GitHub Pages

Set the `API_BASE_URL` variable (step 1), then push to `main` or run
`gh workflow run deploy-pages.yml`. The build fails (by design) if `API_BASE_URL`
is missing or not `https://`; it refuses to publish anything that looks like a secret.

**Staff access in the browser.** The site ships no credentials. A staff member may
type an access token (one of `API_ACCESS_TOKENS`) into the "Staff access" dialog;
it is kept in `sessionStorage` for that tab only and sent as a Bearer header. CORS
uses no cookies (`allow_credentials=false`) and only the Pages origin. Without a
token the API answers from public website content only.

Local preview: `API_BASE_URL=http://localhost:8000 python frontend/build.py && python -m http.server -d frontend/dist 5500`
(add `http://localhost:5500` to `CORS_ALLOW_ORIGINS` in your local `.env`).

## 5. Mattermost

System Console → Integrations → **Slash Commands** → Add:

| Field | Value |
|---|---|
| Command trigger | `askkb` |
| Request URL | `https://<service>.onrender.com/mattermost/ask` |
| Request method | POST |
| Autocomplete | on; hint `[--me\|--user u\|--channel c\|--group a,b] [short\|detailed\|search] question` |

Copy the generated token into Render `MATTERMOST_SLASH_TOKEN`. Create a **Bot
Account** (System Console → Integrations → Bot Accounts), put its token in
`MATTERMOST_BOT_TOKEN`, and add the bot to channels where it should post publicly
or accept `--channel`. Security (unchanged): constant-time slash-token check,
HMAC-signed and channel-bound buttons/dialogs, requester must be a channel member
for `--channel`, guest accounts and channels with guests refused
(`MATTERMOST_BLOCK_GUESTS=true`, fails closed without the bot token).

## 6. Daily refresh

Scheduled — nothing to start. The workflow polls every 30 min and
`scripts/refresh_gate.py` runs the refresh once per day at `KB_REFRESH_TIME` in
`KB_REFRESH_TIMEZONE` (06:00 Asia/Kolkata; timezone-aware, not UTC). Flow:
restore last release → incremental crawl (website + Commit KB) → re-embed changed
chunks only → FAISS + BM25 → validation + smoke test → atomic promotion → encrypted
bundle upload (bundle first, manifest last). A failed run publishes nothing; the
previous bundle stays live. Render picks up a new version within
`KB_SYNC_INTERVAL_MINUTES` (or on its next start).

```bash
gh run list --workflow kb-refresh.yml --limit 5
gh release download kb-latest -p kb-status.json -O - | python -m json.tool
curl https://<service>.onrender.com/rag/status     # kb.version advances after the sync
```

Manual run: `gh workflow run kb-refresh.yml -f force=true` (add `-f full=true` for a full crawl).

## 7. Production smoke test

```bash
python scripts/smoke_test.py --api https://<service>.onrender.com
SMOKE_STAFF_TOKEN=<one staff token> python scripts/smoke_test.py --api https://<service>.onrender.com
```

Checks `/health`, `/ready` (waits for a cold start), KB version, a public query
(website-only citations, citation numbers valid, https URLs, scope + KB version),
anonymous internal query returns no internal sources, invalid token → 401, staff
query → internal scope, Mattermost wrong slash token / unsigned callback → 403,
CORS allow (Pages) / deny (other origins). Exit code 0 = all passed.

Then open the Pages site (header: "Service online · KB <version>"), ask a question,
open a citation; in Mattermost run `/askkb What is the red flag rule?` as staff
(Commit KB citations) and as a guest (refused).

## 8. Rollback

* **Bad KB published:** delete the new assets from `kb-latest` and re-upload the
  previous bundle + manifest (or re-run the refresh); Render keeps serving what it
  last activated and loads the manifest's version on its next sync/start.
* **Bad code:** Render → service → Events/Deploys → roll back to the previous
  deploy (or revert the commit on `main`; auto-deploy rebuilds).

## 9. Local data location (OneDrive)

Generated data lives under `DATA_DIR` (default `<repo>/data`). If the repository
sits inside OneDrive/Dropbox, set `DATA_DIR=C:/takshashila-data` in your local
`.env` (copy `data/releases` + `data/reports` there once; nothing moves
automatically). Render and GitHub Actions are unaffected.

## 10. Other container hosts

The image is portable (Linux, no Windows/OneDrive dependency): build the
`Dockerfile`, set the same environment variables, health-check `/health`, one
instance. `render.yaml` is only read by Render. On hosts with ≥ 1 GB RAM,
`KB_LOW_MEMORY=false` gives zero-downtime KB swaps.
