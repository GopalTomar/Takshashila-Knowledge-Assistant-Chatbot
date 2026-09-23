# Deployment — laptop-independent production

Three pieces, all hosted:

| Piece | Where | What it does |
|---|---|---|
| Frontend | **GitHub Pages** (`.github/workflows/deploy-pages.yml`) | Static UI calling the API |
| API + Mattermost bot | **Railway** (Docker, `railway.json`) | `/api/*`, `/mattermost/*`, health |
| Daily KB refresh | **GitHub Actions** (`.github/workflows/kb-refresh.yml`) | 06:00 Asia/Kolkata crawl → build → validate → publish encrypted KB |

Why the refresh runs in GitHub Actions rather than inside the API: crawling ~1,400
URLs and embedding changed chunks is CPU/memory heavy and must never degrade the
API; Railway volumes cannot be shared between a cron service and the API; Actions
is free for public repos, independent of the API's health, and emails the repo
owner when a run fails. The API only downloads verified, validated releases.

Repository: `GopalTomar/Takshashila-Knowledge-Assistant-Chatbot` (a *project* Pages
repository). Replace `<owner>/<repo>` below with that name. Public site:
**https://gopaltomar.github.io/Takshashila-Knowledge-Assistant-Chatbot/** — the
frontend uses only relative asset paths, so it works under that sub-path.

---

## 0. One-time preparation (local)

```bash
# Generate the bundle encryption key (keep it secret; used by Actions AND Railway)
python -m src.kb_bundle genkey
# Generate one or more staff access tokens for internal (Commit KB) answers on the web UI
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 1. GitHub repository settings

**Settings → Secrets and variables → Actions → Secrets**

| Secret | Value |
|---|---|
| `KB_BUNDLE_KEY` | output of `genkey` |
| `COMMIT_KB_USERNAME` | Commit KB basic-auth username |
| `COMMIT_KB_PASSWORD` | Commit KB basic-auth password |

**Variables**

| Variable | Value |
|---|---|
| `API_BASE_URL` | `https://<your-service>.up.railway.app` (after step 3) |
| `KB_REFRESH_TIME` | `06:00` (optional; default) |
| `KB_REFRESH_TIMEZONE` | `Asia/Kolkata` (optional; default) |
| `KB_REFRESH_ENABLED` | `true` (optional) |

**Settings → Pages → Build and deployment → Source: GitHub Actions.**

**Settings → Actions → General → Workflow permissions: Read and write** (the refresh
job uploads release assets).

## 2. First knowledge-base release

Either let Actions bootstrap it (full crawl on GitHub's servers, ~30–60 min):

```bash
gh workflow run kb-refresh.yml -f force=true -f full=true
gh run watch
```

or publish the release already built locally (faster):

```bash
export KB_BUNDLE_KEY=...            # same value as the secret
python scripts/kb_release.py pack --out-dir dist-kb
gh release create kb-latest --prerelease --title "Knowledge base (latest, encrypted)" --notes "Encrypted KB bundle"
gh release upload kb-latest dist-kb/*.tkkb --clobber
gh release upload kb-latest dist-kb/kb-manifest.json --clobber
```

Check: `gh release view kb-latest` lists `kb-manifest.json` and `kb-<version>.tkkb`.

## 3. Railway (API + Mattermost)

1. New project → **Deploy from GitHub repo** → this repo (branch `main`). Railway
   reads `railway.json` and builds the `Dockerfile`.
2. (Recommended) add a **Volume** mounted at `/app/data` so restarts reuse the
   downloaded KB instead of re-downloading it.
3. **Variables** (never commit these):

| Variable | Value |
|---|---|
| `GROQ_API_KEY` | Groq key |
| `GROQ_MODEL` | `openai/gpt-oss-120b` (verified available on your Groq account) |
| `KB_BUNDLE_KEY` | same as the GitHub secret |
| `KB_BUNDLE_MANIFEST_URL` | `https://github.com/<owner>/<repo>/releases/download/kb-latest/kb-manifest.json` |
| `KB_SYNC_INTERVAL_MINUTES` | `30` |
| `CORS_ALLOW_ORIGINS` | `https://gopaltomar.github.io` (origin only — browsers never send the path; a full Pages URL is reduced to its origin automatically) |
| `API_ACCESS_TOKENS` | comma-separated staff tokens (step 0) |
| `PUBLIC_SOURCES` | `website` |
| `MATTERMOST_URL` | `https://matter.takshashila.org.in` |
| `MATTERMOST_BOT_TOKEN` | bot account token |
| `MATTERMOST_SLASH_TOKEN` | slash command token (step 5) |
| `MATTERMOST_BOT_PUBLIC_URL` | `https://<your-service>.up.railway.app` |
| `MATTERMOST_ACTION_SECRET` | random string (signs buttons/dialogs; recommended) |
| `MATTERMOST_BLOCK_GUESTS` | `true` (default; keeps internal answers away from guests) |

`PORT` is injected by Railway — do not set it. The health check is `/health`.

4. **Settings → Networking → Generate Domain.** Verify:

```bash
curl https://<service>.up.railway.app/health         # liveness (Railway health check)
curl https://<service>.up.railway.app/ready          # 200 once KB + model are loaded, else 503
curl https://<service>.up.railway.app/rag/status     # KB version, vectors, bundle sync, refresh health
curl https://<service>.up.railway.app/api/health     # same status, "ready": true
curl -X POST https://<service>.up.railway.app/api/query -H 'Content-Type: application/json' \
     -d '{"query":"What is Takshashila Institution?"}'
```

Memory: plan for ≥ 2 GB (embedding model + ~33k-vector index + BM25).

## 4. GitHub Pages

Set the `API_BASE_URL` variable (step 1), then push to `main` or run:

```bash
gh workflow run deploy-pages.yml
```

The site is published at `https://gopaltomar.github.io/Takshashila-Knowledge-Assistant-Chatbot/`.
Local preview: `API_BASE_URL=http://localhost:8000 python frontend/build.py && python -m http.server -d frontend/dist 5500`
(add `CORS_ALLOW_ORIGINS=http://localhost:5500` to your local `.env`).

## 5. Mattermost

System Console → Integrations → **Slash Commands** → Add:

| Field | Value |
|---|---|
| Command trigger | `askkb` |
| Request URL | `https://<service>.up.railway.app/mattermost/ask` |
| Request method | POST |
| Autocomplete | on; hint `[--me\|--user u\|--channel c\|--group a,b] [short\|detailed\|search] question` |

Copy the generated token into Railway `MATTERMOST_SLASH_TOKEN`. Create a **Bot
Account** (System Console → Integrations → Bot Accounts), put its token in
`MATTERMOST_BOT_TOKEN`, and add the bot to channels it should post in. If your
Mattermost restricts outgoing connections, allow the Railway domain under
*System Console → Developer → Allow untrusted internal connections*.

## 6. Daily refresh

Nothing to start — the workflow is scheduled. Verify after 06:00 IST:

```bash
gh run list --workflow kb-refresh.yml --limit 5
gh release download kb-latest -p kb-status.json -O - | python -m json.tool
curl https://<service>.up.railway.app/api/health    # kb.version advances within KB_SYNC_INTERVAL_MINUTES
```

Manual run: `gh workflow run kb-refresh.yml -f force=true` (add `-f full=true` for a full crawl).

## 7. Local data location (OneDrive)

Generated data (KB releases ~340 MB each, crawl state, logs, reports) lives under
`DATA_DIR` (default `<repo>/data`). If the repository sits inside OneDrive/Dropbox,
point `DATA_DIR` at a non-synced folder in your local `.env`:

```bash
DATA_DIR=C:/takshashila-data
```

Curated inputs (the holiday list) are read from `<repo>/data/knowledge_base`
regardless (`KB_INPUT_DIR` overrides). To keep the current local KB, copy it once —
nothing is moved automatically:

```powershell
robocopy data\releases C:\takshashila-data\releases /E
robocopy data\reports  C:\takshashila-data\reports  /E
```

Railway and GitHub Actions are unaffected (they use `/app/data` and the runner workspace).

## 8. Rollback

* Bad KB published: re-upload a previous bundle + manifest from a successful run's
  local build, or delete the new assets; the API keeps serving whatever it last
  activated successfully.
* Bad code: redeploy the previous commit in Railway (Deployments → Redeploy).
