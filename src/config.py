"""
config.py — Central configuration for Takshashila RAG
Loads from .env; provides typed constants for the whole project.

Updated: adds Commit KB (authenticated) settings, a dedicated
data/knowledge_base/ area, source-priority ranking, and tunable
retrieval thresholds.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT_DIR        = Path(__file__).resolve().parent.parent
# Generated, private runtime data (KB releases, crawl state, logs, reports).
# Point DATA_DIR at a local, NON-synced directory (e.g. outside OneDrive/Dropbox):
# releases are ~340 MB each and contain internal Commit KB content.
DATA_DIR        = Path(os.path.expandvars(os.path.expanduser(
    os.getenv("DATA_DIR") or str(ROOT_DIR / "data")))).resolve()      # empty value = default
LOGS_DIR        = DATA_DIR / "logs"
REPORTS_DIR     = DATA_DIR / "reports"
RAW_HTML_DIR    = DATA_DIR / "raw" / "html"
RAW_PDF_DIR     = DATA_DIR / "raw" / "pdfs"

# Curated source inputs maintained by hand and tracked in git (holiday list, …).
# Deliberately NOT under DATA_DIR, so moving DATA_DIR never drops these inputs.
KB_DIR          = Path(os.getenv("KB_INPUT_DIR") or str(ROOT_DIR / "data" / "knowledge_base")).resolve()

# ── Switchable KB root ────────────────────────────────────────────────────────────
# Everything the RAG engine *serves* (documents, chunks, FAISS, metadata, crawl
# state, people graph) lives under one "KB root". Production uses DATA_DIR; the
# refresh pipeline builds into a staging root and a server can activate a
# downloaded release root — see use_kb_root(). The derived path constants below
# are re-pointed together so a half-updated mix of files is never read.
KB_ROOT: Path = DATA_DIR
PROCESSED_DIR = DOCUMENTS_FILE = CHUNKS_FILE = INDEX_DIR = FAISS_INDEX = None  # set below
METADATA_FILE = METADATA_JSON = EMBEDDING_CACHE_FILE = STATE_DIR = PEOPLE_FILE = None
KB_MANIFEST_FILE = None


def use_kb_root(root) -> Path:
    """Point every KB path constant at ``root`` (a directory). Returns the root."""
    global KB_ROOT, PROCESSED_DIR, DOCUMENTS_FILE, CHUNKS_FILE, INDEX_DIR, FAISS_INDEX
    global METADATA_FILE, METADATA_JSON, EMBEDDING_CACHE_FILE, STATE_DIR, PEOPLE_FILE
    global KB_MANIFEST_FILE
    KB_ROOT = Path(root)
    PROCESSED_DIR = KB_ROOT / "processed"
    DOCUMENTS_FILE = PROCESSED_DIR / "documents.jsonl"   # unified docs used to build the index
    CHUNKS_FILE = PROCESSED_DIR / "chunks.jsonl"
    PEOPLE_FILE = PROCESSED_DIR / "people.json"          # author/person relationship graph
    INDEX_DIR = KB_ROOT / "index"
    FAISS_INDEX = INDEX_DIR / "faiss.index"
    METADATA_JSON = INDEX_DIR / "metadata.json"
    METADATA_FILE = INDEX_DIR / "metadata.pkl"            # legacy; never loaded by default
    EMBEDDING_CACHE_FILE = INDEX_DIR / "embedding_cache.npz"
    STATE_DIR = KB_ROOT / "state"                         # incremental crawl state
    KB_MANIFEST_FILE = KB_ROOT / "kb_manifest.json"       # version + counts of this KB
    return KB_ROOT


RELEASES_DIR = DATA_DIR / "releases"
CURRENT_POINTER = RELEASES_DIR / "CURRENT"   # one line: the active release dir name


def resolve_active_kb_root() -> Path:
    """
    The KB root the application serves:
      1. KB_ROOT env var (explicit override), else
      2. data/releases/<name in CURRENT> (versioned releases, atomic promotion), else
      3. DATA_DIR (legacy flat layout: data/processed + data/index).
    """
    env = os.getenv("KB_ROOT")
    if env:
        return Path(env)
    try:
        name = CURRENT_POINTER.read_text(encoding="utf-8").strip()
        if name and (RELEASES_DIR / name / "index" / "faiss.index").exists():
            return RELEASES_DIR / name
    except OSError:
        pass
    return DATA_DIR


use_kb_root(resolve_active_kb_root())

SCRAPE_LOG      = LOGS_DIR / "scrape.log"
FAILED_CSV      = LOGS_DIR / "failed_urls.csv"

# Legacy pickle metadata is only read when explicitly allowed (pickle can execute
# code if the file is tampered with; JSON cannot).
ALLOW_PICKLE_METADATA = os.getenv("ALLOW_PICKLE_METADATA", "false").lower() in ("1", "true", "yes")

# ── Commit KB (primary source) ──────────────────────────────────────────────────
# Raw crawl output (produced by scripts/scrape_commit_kb.py)
COMMIT_KB_CRAWL_DIR  = ROOT_DIR / "commit_kb_clean_crawled"
COMMIT_KB_RAW_JSONL  = COMMIT_KB_CRAWL_DIR / "rag_documents.jsonl"

# RAG-ready Commit KB files (produced by scripts/save_commit_kb_to_rag.py)
COMMIT_KB_JSONL      = KB_DIR / "takshashila_commit_kb.jsonl"
COMMIT_KB_INDEX      = KB_DIR / "takshashila_commit_kb_index.json"
COMMIT_KB_METADATA   = KB_DIR / "takshashila_commit_kb_metadata.json"

# Public Takshashila website (publications + blogs) as a first-class KB source.
# Raw crawl output is data/processed/documents.jsonl (produced by src.scraper);
# the RAG-ready, cleaned website KB (produced by scripts/save_website_to_rag.py):
WEBSITE_JSONL     = KB_DIR / "takshashila_website.jsonl"
WEBSITE_INDEX     = KB_DIR / "takshashila_website_index.json"
WEBSITE_METADATA  = KB_DIR / "takshashila_website_metadata.json"

# Optional secondary local sources (folded into the index if present)
LOCAL_DOCUMENTS_FILE = KB_DIR / "local_documents.jsonl"        # secondary
STAFF_HANDBOOK_FILE  = KB_DIR / "takshashila_staff_handbook.jsonl"  # optional supporting

# Curated internal files folded into every index build (src/supplementary.py).
# (file, source key). Missing files are skipped and reported, never an error.
SUPPLEMENTARY_KB_FILES = [
    (KB_DIR / "holiday_list_2026.jsonl", "local"),
    (LOCAL_DOCUMENTS_FILE, "local"),
    (STAFF_HANDBOOK_FILE, "staff_handbook"),
]

# ── Commit KB crawl settings (read from .env; never hardcode credentials) ────────
COMMIT_KB_URL      = os.getenv("COMMIT_KB_URL", "https://commit.takshashila.org.in/")
COMMIT_KB_USERNAME = os.getenv("COMMIT_KB_USERNAME", "")
COMMIT_KB_PASSWORD = os.getenv("COMMIT_KB_PASSWORD", "")

# ── Legacy public-website sources (kept for backward compatibility) ──────────────
PUBLICATIONS_URL = "https://takshashila.org.in/pages/publications/"
BLOGS_URL        = "https://takshashila.org.in/pages/blogs/"

RSS_FEEDS = [
    "https://takshashila.org.in/feed/",
    "https://takshashila.org.in/category/publications/feed/",
    "https://takshashila.org.in/category/blogs/feed/",
]

USER_AGENT = os.getenv(
    "CRAWLER_USER_AGENT",
    "TakshashilaKnowledgeAssistant/2.0 (+https://takshashila.org.in/; internal knowledge-base crawler)",
)

# ── Scraper ────────────────────────────────────────────────────────────────────
SCRAPE_DELAY       = float(os.getenv("SCRAPE_DELAY", "1.0"))
SCRAPE_TIMEOUT     = int(os.getenv("SCRAPE_TIMEOUT", "30"))
SCRAPE_MAX_RETRIES = int(os.getenv("SCRAPE_MAX_RETRIES", "3"))
# Global politeness cap shared by all crawler threads (requests per second).
SCRAPE_MAX_RPS     = float(os.getenv("SCRAPE_MAX_RPS", "4"))

# ── Public website — full-site discovery (sitemap + listings + crawl fallback) ──
WEBSITE_BASE_URL = os.getenv("WEBSITE_BASE_URL", "https://takshashila.org.in/").rstrip("/") + "/"
WEBSITE_DOMAIN   = "takshashila.org.in"

# Candidate sitemap URLs to try (WordPress/Yoast-style sitemap index is checked
# first; each entry may itself be a sitemap index that is expanded recursively).
WEBSITE_SITEMAP_URLS = [
    "https://takshashila.org.in/sitemap.xml",
    "https://takshashila.org.in/sitemap_index.xml",
    "https://takshashila.org.in/wp-sitemap.xml",
]

# Publisher recorded on first-party documents (website, Commit KB, curated files).
ORG_NAME = os.getenv("ORG_NAME", "Takshashila Institution")

# Known listing pages to paginate (beyond publications/blogs), tried politely —
# a 404 on any of these is skipped silently, so it's safe to over-list here.
WEBSITE_LISTING_URLS = [
    ("https://takshashila.org.in/pages/publications/", "publication"),
    ("https://takshashila.org.in/pages/blogs/", "blog"),
    ("https://takshashila.org.in/pages/news/", "op-ed"),
    ("https://takshashila.org.in/pages/team/", "people"),
    ("https://takshashila.org.in/research/", "research"),
    ("https://takshashila.org.in/commentary/", "commentary"),
    ("https://takshashila.org.in/reports/", "report"),
    ("https://takshashila.org.in/papers/", "paper"),
    ("https://takshashila.org.in/briefs/", "brief"),
]

# URL-path substrings that mark a page as "useful content".
#
# IMPORTANT (full-site coverage): these are now used ONLY as an *optional*
# keep-hint, NOT as a discovery gate. The crawler follows EVERY internal link
# (minus the follow-excludes below) so that every page and subpage is visited;
# whether a visited page is *kept* as a knowledge-base document is decided by
# WEBSITE_DOC_EXCLUDE_PATTERNS + the minimum-text-length filter. Leaving this
# list EMPTY means "keep every content page that passes the text filter", which
# is what you want for a complete crawl. It is kept here (populated) only for
# backward compatibility / documentation; the website config below passes an
# empty keep-filter on purpose.
WEBSITE_INCLUDE_PATH_PATTERNS = [
    "/content/", "/publications/", "/blogs/", "/articles/", "/research/",
    "/commentary/", "/policy/", "/papers/", "/reports/", "/briefs/",
]

# ── Two-tier crawl filtering (the key to "scrape every page and subpage") ───────
#
# 1) FOLLOW-exclude — URLs the crawler must NEVER enqueue/visit at all
#    (admin, auth, cart, feeds, JSON APIs, mail/tel links, same-page fragments,
#    share/reply query strings). Everything else internal IS followed, so the
#    BFS reaches the whole site — including pages only linked from category,
#    tag, author or paginated archive pages.
WEBSITE_FOLLOW_EXCLUDE_PATTERNS = [
    "/wp-admin/", "/wp-login", "/wp-json/", "/xmlrpc.php",
    "/cart/", "/checkout/", "/my-account/", "/logout", "/register",
    # (fragments are stripped by canonicalize_url, so "#" links now resolve to
    #  their page instead of being refused outright)
    "?share=", "?replytocom=",
    "/comments/feed/", "/trackback/",
]

# 2) DOC-exclude — URLs that ARE followed (for discovery) but must NEVER be
#    kept as a knowledge-base document (navigation, archives, search, feeds,
#    bare pagination shells). Their outbound links are still crawled so we
#    reach the real content behind them.
WEBSITE_DOC_EXCLUDE_PATTERNS = [
    "/tag/", "/tags/", "/category/", "/categories/", "/author/", "/authors/",
    "/search", "/page/", "/feed/", "/wp-json/", "/wp-admin/",
]

# Listing pages whose entries are ingested one-by-one as "op-ed" reference
# documents (title, authors, outlet, date, external URL). /pages/news/ lists the
# op-eds and media pieces Takshashila staff publish in external outlets.
WEBSITE_OPED_LISTING_PATHS = [p.strip() for p in os.getenv(
    "WEBSITE_OPED_LISTING_PATHS", "/pages/news/").split(",") if p.strip()]

# Back-compat alias (older code referenced WEBSITE_EXCLUDE_PATH_PATTERNS).
WEBSITE_EXCLUDE_PATH_PATTERNS = WEBSITE_DOC_EXCLUDE_PATTERNS

# Non-document file extensions to never fetch as a "page" during crawl fallback
# (still allowed as linked PDFs, handled separately).
WEBSITE_SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".css", ".js",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3", ".zip", ".xml",
)

# Minimum extracted text length for a crawled page to be kept as a document
# (filters out empty/near-empty navigation & chrome pages).
WEBSITE_MIN_TEXT_LEN = int(os.getenv("WEBSITE_MIN_TEXT_LEN", "200"))

# Crawl safety caps (overridable via CLI flags / env). Raised so a first
# full crawl genuinely reaches every page/subpage of the site rather than
# stopping at an artificially low cap. The incremental Tuesday run is cheap
# regardless of these because unchanged pages are skipped.
WEBSITE_MAX_PAGES  = int(os.getenv("WEBSITE_MAX_PAGES", "5000"))
WEBSITE_MAX_DEPTH  = int(os.getenv("WEBSITE_MAX_DEPTH", "8"))
WEBSITE_INCLUDE_PDFS = os.getenv("WEBSITE_INCLUDE_PDFS", "true").lower() in ("1", "true", "yes", "on")

# Concurrency for the crawl thread pool. Kept modest to stay polite to the
# origin server; combined with SCRAPE_DELAY it bounds the request rate.
SCRAPE_MAX_WORKERS = int(os.getenv("SCRAPE_MAX_WORKERS", "8"))

# Manifest of the most recent website scrape run (what was scraped / skipped /
# failed / updated), written fresh on every run of scripts/update_website.py.
WEBSITE_MANIFEST_FILE = LOGS_DIR / "website_scrape_manifest.json"

# ── Embeddings ─────────────────────────────────────────────────────────────────
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_DIM   = 384   # bge-small-en-v1.5; update if model changes

# ── Chunking ───────────────────────────────────────────────────────────────────
# Chunk sizes are expressed in CHARACTERS (the new chunker is character-based,
# which matches the 800–1200 char / 150–250 overlap guidance).
CHUNK_SIZE    = int(os.getenv("CHUNK_SIZE", "1000"))     # target chars per chunk
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))   # overlap chars
CHUNK_MIN_LEN = int(os.getenv("CHUNK_MIN_LEN", "60"))    # drop tiny fragments

# ── Groq ───────────────────────────────────────────────────────────────────────
GROQ_API_KEY  = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL    = os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b"

# Models verified as available on the organisation's Groq account (2026-09-23).
# llama-3.3-70b-versatile / llama-3.1-8b-instant / mixtral / gemma2 are retired
# and return NotFoundError.
AVAILABLE_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
]

# ── RAG retrieval ────────────────────────────────────────────────────────────────
TOP_K             = int(os.getenv("TOP_K", "5"))
DEFAULT_TEMP      = 0.1
MAX_CONTEXT_CHARS = 12000   # rough cap before sending to Groq

# Minimum raw cosine similarity for a chunk to count as "evidence".
# Below this, the pipeline returns an honest "insufficient evidence" reply.
MIN_SCORE_THRESHOLD = float(os.getenv("MIN_SCORE_THRESHOLD", "0.35"))

# Confidence tiers, based on the best raw cosine similarity among hits.
CONF_HIGH_THRESHOLD   = float(os.getenv("CONF_HIGH_THRESHOLD", "0.62"))
CONF_MEDIUM_THRESHOLD = float(os.getenv("CONF_MEDIUM_THRESHOLD", "0.48"))
# (low = MIN_SCORE_THRESHOLD .. CONF_MEDIUM_THRESHOLD ; none = below MIN)

# ── Citation verification (anti-hallucination) ──────────────────────────────────
# After the LLM answers, verify its inline [Source N] citations against the
# retrieved context and return ONLY the sources actually used. This guarantees
# the displayed references exactly match where the answer came from.
VERIFY_CITATIONS = os.getenv("VERIFY_CITATIONS", "true").lower() in ("1", "true", "yes", "on")
# If the model cites nothing verifiable and no answer sentence overlaps the
# retrieved context above this token-overlap ratio, treat the answer as
# ungrounded and return the honest "insufficient evidence" reply instead.
GROUNDING_MIN_OVERLAP = float(os.getenv("GROUNDING_MIN_OVERLAP", "0.18"))

# ── Source priority ──────────────────────────────────────────────────────────────
# Higher number = higher priority. Used to boost ranking when scores are close.
SOURCE_PRIORITY = {
    "commit_kb":      3,   # primary, living knowledge base
    "staff_handbook": 2,   # secondary supporting source
    "website":        2,   # public Takshashila website (all first-party content)
    "local":          2,   # curated internal files (holiday list, …)
}
DEFAULT_SOURCE_PRIORITY = 1   # everything else (pdfs, local, legacy)

# Fractional boost applied per priority tier above the base tier.
# e.g. commit_kb (tier 3) → fused_score * (1 + (3-1)*0.12) = *1.24
SOURCE_PRIORITY_BOOST = float(os.getenv("SOURCE_PRIORITY_BOOST", "0.12"))

# Human-readable names for known sources (used in citations / UI).
SOURCE_DISPLAY_NAMES = {
    "commit_kb":      "Commit — Takshashila Knowledge Base",
    "staff_handbook": "Takshashila Staff Handbook",
    "website":        "Takshashila Website",
    "publication":    "Takshashila Publication",
    "blog":           "Takshashila Blog",
    "pdf":            "Takshashila PDF",
    "local":          "Local Document",
}


def source_priority(source: str) -> int:
    """Return the priority tier for a source string."""
    return SOURCE_PRIORITY.get((source or "").lower(), DEFAULT_SOURCE_PRIORITY)


def source_display_name(source: str, fallback: str = "") -> str:
    """Return a human-readable name for a source key."""
    return SOURCE_DISPLAY_NAMES.get((source or "").lower(), fallback or source or "Source")


# ── Automated daily refresh (scripts/refresh_kb.py) ────────────────────────────
# Default production schedule: every day at 06:00 Asia/Kolkata. The time is ALWAYS
# interpreted in KB_REFRESH_TIMEZONE (never UTC). Used by the GitHub Actions gate
# (scripts/refresh_gate.py) and the optional self-hosted APScheduler runner.
KB_REFRESH_ENABLED  = os.getenv("KB_REFRESH_ENABLED", "true").lower() in ("1", "true", "yes", "on")
KB_REFRESH_TIME     = os.getenv("KB_REFRESH_TIME", "06:00")
KB_REFRESH_TIMEZONE = os.getenv("KB_REFRESH_TIMEZONE", os.getenv("SCHEDULE_TIMEZONE", "Asia/Kolkata"))
# Consecutive failed refreshes after which refresh health is reported "degraded".
KB_REFRESH_DEGRADED_AFTER = int(os.getenv("KB_REFRESH_DEGRADED_AFTER", "3"))
# Safety valve: never apply removals if more than this fraction of fetches failed.
KB_MAX_FAILURE_RATIO = float(os.getenv("KB_MAX_FAILURE_RATIO", "0.2"))
# A URL must be missing (404/410 or unlinked) on this many consecutive complete
# crawls before its document is removed — a single bad day never deletes knowledge.
KB_REMOVAL_CONFIRMATIONS = int(os.getenv("KB_REMOVAL_CONFIRMATIONS", "2"))


def refresh_hour_minute():
    hh, _, mm = (KB_REFRESH_TIME or "06:00").partition(":")
    return int(hh), int(mm or 0)


# Back-compat names used by scripts/scheduler.py
SCHEDULE_DAY       = os.getenv("SCHEDULE_DAY", "*")
SCHEDULE_HOUR, SCHEDULE_MINUTE = refresh_hour_minute()
SCHEDULE_TIMEZONE  = KB_REFRESH_TIMEZONE

# Refresh bookkeeping.
SCHEDULER_LOG      = LOGS_DIR / "scheduler.log"
SCHEDULER_LOCK     = LOGS_DIR / "scheduler.lock"        # single-instance guard
SCHEDULER_STATUS   = LOGS_DIR / "scheduler_status.json" # last-run result, for the UI
DAILY_REPORTS_DIR  = REPORTS_DIR / "daily_refresh"

# ── Distribution of the built KB to the API (see src/kb_bundle.py, src/kb_sync.py) ──
KB_BUNDLE_MANIFEST_URL = os.getenv("KB_BUNDLE_MANIFEST_URL", "")
KB_BUNDLE_KEY          = os.getenv("KB_BUNDLE_KEY", "")          # Fernet key; never commit
KB_BUNDLE_TOKEN        = os.getenv("KB_BUNDLE_TOKEN", "")        # optional GitHub token
KB_SYNC_INTERVAL_MINUTES = int(os.getenv("KB_SYNC_INTERVAL_MINUTES", "30"))


# ── Production API (api/main.py) ─────────────────────────────────────────────────
def _csv(name: str, default: str = "") -> list:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


# Browser origins allowed to call the API (e.g. https://<user>.github.io). Empty = none.
def _origin(value: str) -> str:
    """
    Reduce a URL to a CORS origin (scheme://host[:port]). Browsers send the Origin
    header without a path, so a project-Pages URL such as
    https://user.github.io/Repo-Name/ must be allowed as https://user.github.io.
    """
    from urllib.parse import urlsplit
    v = value.strip()
    if v == "*":
        return v
    parts = urlsplit(v)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}" if parts.scheme and parts.netloc else v.rstrip("/")


CORS_ALLOW_ORIGINS = list(dict.fromkeys(_origin(o) for o in _csv("CORS_ALLOW_ORIGINS")))
# Bearer tokens that unlock INTERNAL sources (Commit KB, local files) on the API.
# Without a valid token the API answers only from PUBLIC_SOURCES.
API_ACCESS_TOKENS  = _csv("API_ACCESS_TOKENS")
PUBLIC_SOURCES     = _csv("PUBLIC_SOURCES", "website")
INTERNAL_SOURCES   = _csv("INTERNAL_SOURCES", "commit_kb,website,local,staff_handbook")
API_RATE_LIMIT_PER_MINUTE = int(os.getenv("API_RATE_LIMIT_PER_MINUTE", "30"))
API_QUERY_TIMEOUT_SECONDS = float(os.getenv("API_QUERY_TIMEOUT_SECONDS", "60"))
GROQ_TIMEOUT_SECONDS      = float(os.getenv("GROQ_TIMEOUT_SECONDS", "45"))
# Streamlit admin tab password (Build & Update / Automation). Empty = admin hidden.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
# Log raw query text? Off by default (queries can contain sensitive content).
LOG_QUERY_TEXT = os.getenv("LOG_QUERY_TEXT", "false").lower() in ("1", "true", "yes")


# ── Ensure dirs exist ──────────────────────────────────────────────────────────
def ensure_dirs():
    for d in [LOGS_DIR, REPORTS_DIR, KB_DIR, PROCESSED_DIR, INDEX_DIR]:
        d.mkdir(parents=True, exist_ok=True)

ensure_dirs()