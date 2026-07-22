# Takshashila Knowledge Base RAG

A Streamlit RAG (retrieval-augmented generation) assistant that answers questions
about Takshashila using the authenticated **Commit Knowledge Base** as the primary
source, with the Staff Handbook and existing/local documents as supporting sources.

Answers are grounded in retrieved text and cited. When the knowledge base does not
contain enough evidence, the assistant says so explicitly instead of guessing.

## Architecture

- **Embeddings:** `sentence-transformers` (`BAAI/bge-small-en-v1.5`, 384-dim)
- **Vector store:** FAISS (cosine / inner-product on normalised vectors)
- **Retrieval:** hybrid BM25 + FAISS with Reciprocal Rank Fusion, plus a
  source-priority boost (Commit KB > Staff Handbook > everything else)
- **Generation:** Groq (`llama-3.3-70b-versatile` by default)
- **UI:** Streamlit, light maroon/gold theme

### Loading & caching architecture

The app is built to scale to 50k+ chunks without `MemoryError`:

- Heavy singletons — the FAISS index, chunk metadata, embedding model and BM25
  index — live behind `@st.cache_resource` and are loaded **once per process**,
  then reused across every query and rerun. They are never pickled.
- The small unified `documents.jsonl` (~1k docs) is loaded once via
  `@st.cache_resource`; `@st.cache_data` is reserved for tiny summaries only.
- The large `chunks.jsonl` is **never** loaded whole into RAM just for stats — a
  streaming summariser (`utils.summarize_chunks_file`) returns a small dict.
- BM25 is built over the **same** in-memory metadata list FAISS uses, so there is
  exactly one copy of the chunk text in memory rather than two or three.
- The first query warms everything behind a friendly `st.status`; follow-up
  queries only embed the user's question and are much faster. Answer timing uses
  `time.perf_counter()` and is stored per-message, so it stays accurate (even
  for 30s+ answers) across reruns.

### Source priority

| Source          | Priority | Role                          |
|-----------------|:-------: |-------------------------------|
| Commit KB       | 3        | Primary, living knowledge base|
| Staff Handbook  | 2        | Optional supporting source    |
| Local / website | 1        | Secondary                     |

## Project layout

```
takshashila-rag/
├── app.py                          # Streamlit dashboard
├── requirements.txt
├── .env.example                    # copy to .env and fill in
├── commit_kb_clean_crawled/        # raw crawl output (rag_documents.jsonl, index.json, txt/, json/)
├── data/
│   ├── knowledge_base/             # RAG-ready Commit KB + (optional) handbook / local docs
│   ├── processed/                  # unified documents.jsonl + chunks.jsonl
│   └── index/                      # faiss.index + metadata.pkl + metadata.json
├── scripts/
│   ├── scrape_commit_kb.py         # authenticated crawler (Basic Auth from .env)
│   ├── save_commit_kb_to_rag.py    # clean / normalise / drop nav pages → RAG-ready
│   ├── build_knowledge_base.py     # load → chunk → embed → build FAISS index
│   └── validate_kb.py              # sanity-check the ingested KB
└── src/                            # config, extractors, chunker, vector_store,
                                    # retriever, rag_pipeline, ui_components, …
```

## 1. Install

```bash
pip install -r requirements.txt
```

## 2. Configure `.env`

Copy the template and fill in your values:

```bash
cp .env.example .env
```

The Commit KB requires Basic Auth — set these (never hard-code them):

```
COMMIT_KB_URL=https://commit.takshashila.org.in/
COMMIT_KB_USERNAME=your_username_here
COMMIT_KB_PASSWORD=your_password_here

GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=llama-3.3-70b-versatile
```

## 3. Build the knowledge base

Run these in order the first time:

```bash
pip install -r requirements.txt
python scripts/scrape_commit_kb.py        # crawl the Commit KB (uses Basic Auth from .env)
python scripts/save_commit_kb_to_rag.py   # clean + normalise → data/knowledge_base/
python scripts/scrape.py                  # crawl the public website (publications + blogs)
python scripts/save_website_to_rag.py     # clean + normalise website → data/knowledge_base/
python scripts/validate_kb.py             # sanity-check the ingested KB
python scripts/build_knowledge_base.py    # chunk + embed + build the FAISS index (all sources)
streamlit run app.py                      # launch the dashboard
```

The public **website** (takshashila.org.in publications + blogs) is now a
first-class source alongside the Commit KB. `save_website_to_rag.py` writes a
clean `data/knowledge_base/takshashila_website.jsonl` (+ index/metadata), and
`build_knowledge_base.py` folds it into the FAISS index automatically.

What each step does:

1. **`scrape_commit_kb.py`** — crawls same-domain Commit KB pages with Basic Auth,
   fixes encoding with `ftfy`, and writes clean `.txt` / `.json` per page plus
   `commit_kb_clean_crawled/rag_documents.jsonl` and `index.json`. Prints a crawl summary.
2. **`save_commit_kb_to_rag.py`** — reads the crawl output, strips navigation/footer
   boilerplate, drops index pages (`browse.html`, `/playbook/`, `/decisions/`, etc.),
   assigns IDs like `commit_kb_0001`, and writes
   `data/knowledge_base/takshashila_commit_kb.jsonl` (+ `_index.json`, `_metadata.json`).
3. **`validate_kb.py`** — prints total docs, source/category counts, shortest/longest
   docs, sample titles, and warns about missing metadata or duplicate URLs.
4. **`build_knowledge_base.py`** — loads Commit KB (primary) + Staff Handbook (if present)
   + local/legacy docs, chunks them, embeds with sentence-transformers, and builds the
   FAISS index. Use `--no-local` to index Commit KB (+ handbook) only.

You can also run steps 2 and 4 from inside the app — see the **🛠️ Build / Update KB**
tab (**Ingest Commit KB** and **Build / Rebuild Index**). Scraping (step 1) stays on the
command line because it needs your credentials.

## 4. Run the app

```bash
streamlit run app.py
```

Tabs: **Home** (KB summary), **Build / Update KB**, **Ask Takshashila** (chat with
citations + confidence), **Browse Sources**, **Analytics**.

## 5. Updating the KB later

**Commit KB** — when new Commit pages are added:

```bash
python scripts/scrape_commit_kb.py
python scripts/save_commit_kb_to_rag.py
python scripts/validate_kb.py
python scripts/build_knowledge_base.py
```

**Website** — when the public site publishes new work, do the whole cycle in one
command (scrape → ingest → rebuild the index):

```bash
python scripts/update_website.py             # incremental scrape + ingest + rebuild
python scripts/update_website.py --full      # re-scrape everything
python scripts/update_website.py --ingest-only  # skip scrape; ingest + rebuild
```

### Run-time updates from the Streamlit dashboard

Both sources can be refreshed from the **🛠️ Build / Update KB** tab without
leaving the app. Add an **Update Website** button next to the existing Commit KB
controls:

```python
# app.py — inside the Build / Update KB tab
from scripts.update_website import update_website

if st.button("🌐 Update Website (scrape + ingest + rebuild)"):
    log = st.empty(); lines = []
    def cb(msg):
        lines.append(msg); log.code("\n".join(lines[-25:]))
    summary = update_website(progress_cb=cb)          # incremental by default
    if summary["ok"]:
        st.success(f"Website updated — {summary['ingested']['kept']} docs; "
                   f"{summary['index']['chunks']} chunks indexed.")
    else:
        st.error(summary["error"])
```

Prefer scrape-and-ingest without a rebuild? Call
`update_website(rebuild_index=False, progress_cb=cb)` and rebuild later.

<details><summary>Old low-level cycle (still works)</summary>

```bash
python scripts/scrape.py
python scripts/save_website_to_rag.py
python scripts/validate_kb.py
python scripts/build_knowledge_base.py
```

Or, from the app: **🛠️ Build / Update KB → Ingest Commit KB → Build / Rebuild Index**
(after re-running the scraper in a terminal).

</details>

## Reference accuracy & anti-hallucination

Every answer is checked against the exact passages it was built from **before**
it is shown:

- The model must answer only from retrieved context and cite inline as
  `[Source N]`. After generation, `src/citations.py` keeps **only the sources
  the answer actually cited**, drops retrieved-but-uncited passages, and
  renumbers the answer so the displayed references line up 1-to-1 with where the
  answer came from. No stray or mismatched references.
- If the answer cites nothing verifiable **and** overlaps the retrieved context
  too little (`GROUNDING_MIN_OVERLAP`, default `0.18`), it is treated as
  ungrounded and replaced with the honest "insufficient evidence" reply — so the
  bot never shows a fabricated answer with fake citations.
- Confidence is capped by the best **cited** source's similarity, so a weakly
  supported citation can't be reported as high confidence.

Toggle with `VERIFY_CITATIONS` (default `true`) in `.env`.

## Repairing Encoding / Mojibake in Existing KB

Older Takshashila website/publication data was stored with two distinct problems
that the pipeline now handles differently:

1. **True mojibake** — UTF-8 bytes mis-decoded as Windows-1252, e.g. `Indiaâ€™s` →
   `India's`, `JPÂ¥43` → `JP¥43`, `â€œquotedâ€` → `"quoted"`. This is **repaired**.
2. **OCR / PDF extraction garbage** — unrecoverable noise such as `®ØÙÚÛÜ` or
   `Õ6ÖiÀÁ¬É ÅרÙÚÛmÇ×iÜÓ2ÝÞßà`. This **cannot** be repaired, so the affected
   lines are dropped and logged (never silently deleted).

Crucially, **valid accented words are preserved** — Alcântara, Grâce, Duchâtel,
Boétie, décideurs, réseau are real Unicode and are never treated as mojibake or
garbage. Detection keys on specific broken byte-pair sequences (`â€™`, `Ã¢`, `Â¥`,
`ðŸ`, `�`, …) and on per-line symbol/noise ratio — not on bare accented letters.

The full pipeline (scrape → load → chunk → embed → retrieve → display) repairs
encoding automatically and skips garbage chunks, so newly scraped data stays clean.
To fix data that was **already** scraped and indexed, run:

```bash
pip install -r requirements.txt
python scripts/repair_existing_kb_encoding.py --overwrite
python scripts/validate_kb.py
python scripts/build_knowledge_base.py
python scripts/validate_kb.py
streamlit run app.py
```

What each step does:

1. **`repair_existing_kb_encoding.py`** scans `data/processed/documents.jsonl`,
   `data/processed/chunks.jsonl`, `data/index/metadata.json`,
   `data/knowledge_base/`, and `commit_kb_clean_crawled/` (types `.json .jsonl .csv
   .txt .md`). It repairs mojibake in place and removes unrecoverable OCR-garbage
   lines, logging every change to **`data/logs/quarantined_dirty_text.csv`** (source
   file, document id, title, url, field, bad snippet, reason, action). Run without
   `--overwrite` first for a safe dry-run; `--overwrite` writes a `.bak_<timestamp>`
   backup of each changed file.
2. **`validate_kb.py`** distinguishes (A) real mojibake, (B) valid accented Unicode
   that is allowed, and (C) OCR garbage. It **passes** when there is no real mojibake
   anywhere and the indexed chunks/metadata contain no leftover garbage.
3. **`build_knowledge_base.py`** cleans every document and chunk, drops garbage lines,
   and **quarantines** any still-corrupted chunk to **`data/logs/skipped_dirty_chunks.csv`**
   so it never enters FAISS — then rebuilds the index. This indexes the **full** KB
   (Commit KB + legacy website + PDFs) minus only the unrecoverable chunks. You never
   need `--no-local`.

> **Important:** rebuild the index after repairing. The binary `faiss.index` and
> `metadata.pkl` are generated from the chunk text, so old vectors can still contain
> mojibake until you re-run `build_knowledge_base.py`. The repair script intentionally
> does not edit those binaries — rebuilding regenerates them from the cleaned chunks.

## Example questions

- What are the meeting rules at Takshashila?
- What is the flag system?
- What are the core competencies expected from staff?
- What is Takshashila's approach to AI use?
- What are the rules for sharing docs for review?
- What is the first week checklist?

## Notes

- **Staff Handbook (optional):** drop a JSONL at
  `data/knowledge_base/takshashila_staff_handbook.jsonl` (same schema as the Commit KB
  file) and rebuild — it is picked up automatically as a supporting source.
- **Mattermost:** intended to be ingested later via API only, not UI scraping.
- Credentials are read from environment variables only and are never written to disk
  by these scripts.# Takshashila Knowledge Base RAG

A Streamlit RAG (retrieval-augmented generation) assistant that answers questions
about Takshashila using the authenticated **Commit Knowledge Base** as the primary
source, with the Staff Handbook and existing/local documents as supporting sources.

Answers are grounded in retrieved text and cited. When the knowledge base does not
contain enough evidence, the assistant says so explicitly instead of guessing.

The same RAG engine also powers a **Mattermost slash-command bot** (`/askkb`) that
delivers grounded answers privately, to a colleague, or to a channel — see
[Mattermost assistant](#mattermost-assistant-askkb) below and the full guide in
[`README_MATTERMOST_BOT.md`](README_MATTERMOST_BOT.md).

## Architecture

- **Embeddings:** `sentence-transformers` (`BAAI/bge-small-en-v1.5`, 384-dim)
- **Vector store:** FAISS (cosine / inner-product on normalised vectors)
- **Retrieval:** hybrid BM25 + FAISS with Reciprocal Rank Fusion, plus a
  source-priority boost (Commit KB > Staff Handbook > everything else)
- **Generation:** Groq (`llama-3.3-70b-versatile` by default)
- **UI:** Streamlit, light maroon/gold theme
- **Chat surface:** a FastAPI Mattermost bot in `integrations/` that reuses the
  **same** `src.rag_pipeline.answer()` — the pipeline is never rewritten.

### Loading & caching architecture

The app is built to scale to 50k+ chunks without `MemoryError`:

- Heavy singletons — the FAISS index, chunk metadata, embedding model and BM25
  index — live behind `@st.cache_resource` and are loaded **once per process**,
  then reused across every query and rerun. They are never pickled.
- The small unified `documents.jsonl` (~1k docs) is loaded once via
  `@st.cache_resource`; `@st.cache_data` is reserved for tiny summaries only.
- The large `chunks.jsonl` is **never** loaded whole into RAM just for stats — a
  streaming summariser (`utils.summarize_chunks_file`) returns a small dict.
- BM25 is built over the **same** in-memory metadata list FAISS uses, so there is
  exactly one copy of the chunk text in memory rather than two or three.
- The first query warms everything behind a friendly `st.status`; follow-up
  queries only embed the user's question and are much faster. Answer timing uses
  `time.perf_counter()` and is stored per-message, so it stays accurate (even
  for 30s+ answers) across reruns.

### Source priority

| Source          | Priority | Role                          |
|-----------------|:-------: |-------------------------------|
| Commit KB       | 3        | Primary, living knowledge base|
| Staff Handbook  | 2        | Optional supporting source    |
| Local / website | 1        | Secondary                     |

## Project layout

```
takshashila-rag/
├── app.py                          # Streamlit dashboard
├── requirements.txt
├── .env.example                    # copy to .env and fill in
├── commit_kb_clean_crawled/        # raw crawl output (rag_documents.jsonl, index.json, txt/, json/)
├── data/
│   ├── knowledge_base/             # RAG-ready Commit KB + (optional) handbook / local docs
│   ├── processed/                  # unified documents.jsonl + chunks.jsonl
│   ├── index/                      # faiss.index + metadata.pkl + metadata.json
│   └── logs/                       # mattermost_bot.log, mattermost_feedback.jsonl, …
├── scripts/
│   ├── scrape_commit_kb.py         # authenticated crawler (Basic Auth from .env)
│   ├── save_commit_kb_to_rag.py    # clean / normalise / drop nav pages → RAG-ready
│   ├── build_knowledge_base.py     # load → chunk → embed → build FAISS index
│   └── validate_kb.py              # sanity-check the ingested KB
├── src/                            # config, extractors, chunker, vector_store,
│                                   # retriever, rag_pipeline, ui_components, …  (RAG engine)
├── integrations/                   # Mattermost bot — reuses src/ without modifying it
│   ├── mattermost_bot.py           #   FastAPI transport + endpoints + buttons
│   ├── formatting.py               #   clean message builder (presentation)
│   ├── feedback.py                 #   👍/👎 feedback log
│   ├── command_parser.py           #   parses --me/--user/--channel/--group
│   ├── mattermost_api.py           #   low-level Mattermost REST lookups
│   ├── response_router.py          #   decides WHERE an answer goes
│   └── destination_handlers/       #   dm / user / channel / group handlers
├── tests/                          # test_retrieval, test_chunking, test_command_parser
├── README.md                       # this file
└── README_MATTERMOST_BOT.md        # full Mattermost bot guide
```

## 1. Install

```bash
pip install -r requirements.txt
```

## 2. Configure `.env`

Copy the template and fill in your values:

```bash
cp .env.example .env
```

The Commit KB requires Basic Auth — set these (never hard-code them):

```
COMMIT_KB_URL=https://commit.takshashila.org.in/
COMMIT_KB_USERNAME=your_username_here
COMMIT_KB_PASSWORD=your_password_here

GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=llama-3.3-70b-versatile
```

Your existing Groq and Commit KB variables stay exactly as they are. Mattermost
bot variables (all optional beyond the token/URL) are documented in
[`README_MATTERMOST_BOT.md`](README_MATTERMOST_BOT.md).

## 3. Build the knowledge base

Run these in order the first time:

```bash
pip install -r requirements.txt
python scripts/scrape_commit_kb.py        # crawl the Commit KB (uses Basic Auth from .env)
python scripts/save_commit_kb_to_rag.py   # clean + normalise → data/knowledge_base/
python scripts/scrape.py                  # crawl the public website (publications + blogs)
python scripts/save_website_to_rag.py     # clean + normalise website → data/knowledge_base/
python scripts/validate_kb.py             # sanity-check the ingested KB
python scripts/build_knowledge_base.py    # chunk + embed + build the FAISS index (all sources)
streamlit run app.py                      # launch the dashboard
```

The public **website** (takshashila.org.in publications + blogs) is now a
first-class source alongside the Commit KB. `save_website_to_rag.py` writes a
clean `data/knowledge_base/takshashila_website.jsonl` (+ index/metadata), and
`build_knowledge_base.py` folds it into the FAISS index automatically.

What each step does:

1. **`scrape_commit_kb.py`** — crawls same-domain Commit KB pages with Basic Auth,
   fixes encoding with `ftfy`, and writes clean `.txt` / `.json` per page plus
   `commit_kb_clean_crawled/rag_documents.jsonl` and `index.json`. Prints a crawl summary.
2. **`save_commit_kb_to_rag.py`** — reads the crawl output, strips navigation/footer
   boilerplate, drops index pages (`browse.html`, `/playbook/`, `/decisions/`, etc.),
   assigns IDs like `commit_kb_0001`, and writes
   `data/knowledge_base/takshashila_commit_kb.jsonl` (+ `_index.json`, `_metadata.json`).
3. **`validate_kb.py`** — prints total docs, source/category counts, shortest/longest
   docs, sample titles, and warns about missing metadata or duplicate URLs.
4. **`build_knowledge_base.py`** — loads Commit KB (primary) + Staff Handbook (if present)
   + local/legacy docs, chunks them, embeds with sentence-transformers, and builds the
   FAISS index. Use `--no-local` to index Commit KB (+ handbook) only.

You can also run steps 2 and 4 from inside the app — see the **🛠️ Build / Update KB**
tab (**Ingest Commit KB** and **Build / Rebuild Index**). Scraping (step 1) stays on the
command line because it needs your credentials.

## 4. Run the app

```bash
streamlit run app.py
```

Tabs: **Home** (KB summary), **Build / Update KB**, **Ask Takshashila** (chat with
citations + confidence), **Browse Sources**, **Analytics**.

## Mattermost assistant (`/askkb`)

The same knowledge base is available in Mattermost as a slash-command bot. It
runs as a small FastAPI service in `integrations/` and calls the **existing**
`src.rag_pipeline.answer()` — retrieval is never rewritten, and the Streamlit app
is unaffected.

```
/askkb What is the leave policy?                default → private DM to you
/askkb --user abhishek.k Leave policy           → a colleague's direct messages
/askkb --channel research Explain the rules      → into a channel (bot must be a member)
/askkb --group a,b,c Leave policy                → a group DM (opt-in)
```

Highlights:

- **Retrieval and delivery are separated.** A `command_parser` decides the
  destination, a `response_router` dispatches to per-target handlers
  (`dm / user / channel / group`), and the retrieval engine never knows where the
  answer goes.
- **Private by default** — each person's answer is DM'd to them, so shared
  channels don't fill with everyone's Q&A. `public` / `private` / `short` /
  `detailed` / `search` / `voice` modifiers all still work.
- **Share without re-running RAG** — 👤/📢 **Share** buttons under any answer open
  a dialog and re-deliver the cached answer. **⬇️ Download PDF** and
  **📄 Export Markdown** likewise reuse the generated answer.
- **🔗 Related Policies is grounded** — it shows *only the documents the answer
  was drawn from* (the cited sources), never a broad keyword search, so it can't
  surface irrelevant references.
- **Graceful, validated delivery** — unknown user/channel, a bot that isn't a
  channel member, or a missing group member each fail with a clear, private
  message. Group DMs are **off by default** (`MATTERMOST_ENABLE_GROUP`).

Start it with:

```bash
uvicorn integrations.mattermost_bot:app --host 0.0.0.0 --port 8000
```

Full setup (Bot Account, Slash Command, env vars, buttons, deployment,
troubleshooting, testing) is in **[`README_MATTERMOST_BOT.md`](README_MATTERMOST_BOT.md)**.

## 5. Updating the KB later

**Commit KB** — when new Commit pages are added:

```bash
python scripts/scrape_commit_kb.py
python scripts/save_commit_kb_to_rag.py
python scripts/validate_kb.py
python scripts/build_knowledge_base.py
```

**Website** — when the public site publishes new work, do the whole cycle in one
command (scrape → ingest → rebuild the index):

```bash
python scripts/update_website.py             # incremental scrape + ingest + rebuild
python scripts/update_website.py --full      # re-scrape everything
python scripts/update_website.py --ingest-only  # skip scrape; ingest + rebuild
```

### Run-time updates from the Streamlit dashboard

Both sources can be refreshed from the **🛠️ Build / Update KB** tab without
leaving the app. Add an **Update Website** button next to the existing Commit KB
controls:

```python
# app.py — inside the Build / Update KB tab
from scripts.update_website import update_website

if st.button("🌐 Update Website (scrape + ingest + rebuild)"):
    log = st.empty(); lines = []
    def cb(msg):
        lines.append(msg); log.code("\n".join(lines[-25:]))
    summary = update_website(progress_cb=cb)          # incremental by default
    if summary["ok"]:
        st.success(f"Website updated — {summary['ingested']['kept']} docs; "
                   f"{summary['index']['chunks']} chunks indexed.")
    else:
        st.error(summary["error"])
```

Prefer scrape-and-ingest without a rebuild? Call
`update_website(rebuild_index=False, progress_cb=cb)` and rebuild later.

<details><summary>Old low-level cycle (still works)</summary>

```bash
python scripts/scrape.py
python scripts/save_website_to_rag.py
python scripts/validate_kb.py
python scripts/build_knowledge_base.py
```

Or, from the app: **🛠️ Build / Update KB → Ingest Commit KB → Build / Rebuild Index**
(after re-running the scraper in a terminal).

</details>

## Reference accuracy & anti-hallucination

Every answer is checked against the exact passages it was built from **before**
it is shown:

- The model must answer only from retrieved context and cite inline as
  `[Source N]`. After generation, `src/citations.py` keeps **only the sources
  the answer actually cited**, drops retrieved-but-uncited passages, and
  renumbers the answer so the displayed references line up 1-to-1 with where the
  answer came from. No stray or mismatched references.
- If the answer cites nothing verifiable **and** overlaps the retrieved context
  too little (`GROUNDING_MIN_OVERLAP`, default `0.18`), it is treated as
  ungrounded and replaced with the honest "insufficient evidence" reply — so the
  bot never shows a fabricated answer with fake citations.
- Confidence is capped by the best **cited** source's similarity, so a weakly
  supported citation can't be reported as high confidence.

These verified sources are exactly what the Mattermost bot's **🔗 Related
Policies** button re-uses — it shows the answer's grounding documents, never a
fresh search — so references stay honest across both surfaces.

Toggle with `VERIFY_CITATIONS` (default `true`) in `.env`.

## Repairing Encoding / Mojibake in Existing KB

Older Takshashila website/publication data was stored with two distinct problems
that the pipeline now handles differently:

1. **True mojibake** — UTF-8 bytes mis-decoded as Windows-1252, e.g. `Indiaâ€™s` →
   `India's`, `JPÂ¥43` → `JP¥43`, `â€œquotedâ€` → `"quoted"`. This is **repaired**.
2. **OCR / PDF extraction garbage** — unrecoverable noise such as `®ØÙÚÛÜ` or
   `Õ6ÖiÀÁ¬É ÅרÙÚÛmÇ×iÜÓ2ÝÞßà`. This **cannot** be repaired, so the affected
   lines are dropped and logged (never silently deleted).

Crucially, **valid accented words are preserved** — Alcântara, Grâce, Duchâtel,
Boétie, décideurs, réseau are real Unicode and are never treated as mojibake or
garbage. Detection keys on specific broken byte-pair sequences (`â€™`, `Ã¢`, `Â¥`,
`ðŸ`, `�`, …) and on per-line symbol/noise ratio — not on bare accented letters.

The full pipeline (scrape → load → chunk → embed → retrieve → display) repairs
encoding automatically and skips garbage chunks, so newly scraped data stays clean.
To fix data that was **already** scraped and indexed, run:

```bash
pip install -r requirements.txt
python scripts/repair_existing_kb_encoding.py --overwrite
python scripts/validate_kb.py
python scripts/build_knowledge_base.py
python scripts/validate_kb.py
streamlit run app.py
```

What each step does:

1. **`repair_existing_kb_encoding.py`** scans `data/processed/documents.jsonl`,
   `data/processed/chunks.jsonl`, `data/index/metadata.json`,
   `data/knowledge_base/`, and `commit_kb_clean_crawled/` (types `.json .jsonl .csv
   .txt .md`). It repairs mojibake in place and removes unrecoverable OCR-garbage
   lines, logging every change to **`data/logs/quarantined_dirty_text.csv`** (source
   file, document id, title, url, field, bad snippet, reason, action). Run without
   `--overwrite` first for a safe dry-run; `--overwrite` writes a `.bak_<timestamp>`
   backup of each changed file.
2. **`validate_kb.py`** distinguishes (A) real mojibake, (B) valid accented Unicode
   that is allowed, and (C) OCR garbage. It **passes** when there is no real mojibake
   anywhere and the indexed chunks/metadata contain no leftover garbage.
3. **`build_knowledge_base.py`** cleans every document and chunk, drops garbage lines,
   and **quarantines** any still-corrupted chunk to **`data/logs/skipped_dirty_chunks.csv`**
   so it never enters FAISS — then rebuilds the index. This indexes the **full** KB
   (Commit KB + legacy website + PDFs) minus only the unrecoverable chunks. You never
   need `--no-local`.

> **Important:** rebuild the index after repairing. The binary `faiss.index` and
> `metadata.pkl` are generated from the chunk text, so old vectors can still contain
> mojibake until you re-run `build_knowledge_base.py`. The repair script intentionally
> does not edit those binaries — rebuilding regenerates them from the cleaned chunks.

## Example questions

- What are the meeting rules at Takshashila?
- What is the flag system?
- What are the core competencies expected from staff?
- What is Takshashila's approach to AI use?
- What are the rules for sharing docs for review?
- What is the first week checklist?

## Notes

- **Staff Handbook (optional):** drop a JSONL at
  `data/knowledge_base/takshashila_staff_handbook.jsonl` (same schema as the Commit KB
  file) and rebuild — it is picked up automatically as a supporting source.
- **Mattermost:** answers are delivered through the `/askkb` bot (see
  [`README_MATTERMOST_BOT.md`](README_MATTERMOST_BOT.md)); Mattermost content itself is
  intended to be ingested later via API only, not UI scraping.
- Credentials are read from environment variables only and are never written to disk
  by these scripts.