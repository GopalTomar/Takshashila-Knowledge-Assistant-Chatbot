# Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/api/health` → `"status": "starting"` for minutes | first boot is downloading/decrypting the KB bundle and building BM25 | wait; check `kb_sync.last_error`; ensure ≥2 GB RAM |
| `/api/health` → `"degraded"`, `error: FileNotFoundError … faiss.index` | no KB locally and bundle sync not configured | set `KB_BUNDLE_MANIFEST_URL` + `KB_BUNDLE_KEY`, or mount a volume with a built KB |
| `kb_sync.last_error: InvalidTag` / `checksum mismatch` | wrong `KB_BUNDLE_KEY` or corrupted upload | use the same key as the GitHub secret; re-run the refresh workflow |
| Browser: CORS error | frontend origin not allowed | `CORS_ALLOW_ORIGINS=https://<user>.github.io` on Railway (origin only, no path) |
| Web UI: "Your access token was not accepted" | token not in `API_ACCESS_TOKENS` | fix the Railway variable or remove the token in the UI |
| Web UI answers never cite Commit KB | expected without a staff token (public scope) | enter a staff token via "Staff access" |
| Every answer is "insufficient evidence" | Groq key missing/invalid, or retrieval empty | `/api/health` → `llm_configured`; test `mode: "search"` (no LLM) |
| 504 from `/api/query` | LLM slow | raise `API_QUERY_TIMEOUT_SECONDS`; use `short` mode |
| `/askkb` → "invalid token" (403) | `MATTERMOST_SLASH_TOKEN` mismatch | copy the token from the slash command settings |
| Buttons reply 403 | callback not signed (old post) or `MATTERMOST_ACTION_SECRET` changed | ask the question again for fresh buttons |
| Buttons do nothing | `MATTERMOST_BOT_PUBLIC_URL` unset or Mattermost can't reach Railway | set it to the Railway HTTPS URL; allow outgoing connections in Mattermost |
| Refresh run `partial` with Commit KB 401 | Commit KB password changed | update the `COMMIT_KB_PASSWORD` secret |
| Refresh refuses to promote: "Document count dropped" | crawl returned far fewer pages | inspect `reports/crawl/website_latest.json`; see OPERATIONS.md |
| Refresh workflow never runs | Actions disabled, or `KB_REFRESH_ENABLED=false` | enable Actions; check the gate step output (`reason=`) |
| Local embedding very slow / killed | CPU-only, laptop sleep | embedding is checkpointed every 2,048 chunks — just re-run; it resumes from cache |
| `getaddrinfo failed` loading the model | no network to Hugging Face | the loader falls back to the local cache; containers run with `HF_HUB_OFFLINE=1` |
| Windows console `UnicodeEncodeError` | cp1252 console | `set PYTHONIOENCODING=utf-8` |
| Local disk fills / OneDrive syncing hundreds of MB, or "Access is denied" pruning releases | the repo is inside OneDrive: each KB release (~340 MB) syncs to the cloud and OneDrive marks folders read-only | set `DATA_DIR=C:\takshashila-data` (outside OneDrive) in `.env`; pruning now clears read-only attributes |
