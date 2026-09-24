"""
embeddings.py — Local embeddings for FAISS indexing and queries.

sentence-transformers (torch) by default; EMBEDDING_BACKEND=onnx runs the same model
exported to ONNX so the production API needs no torch (see OnnxEmbedder).
"""

import os
from typing import List

import numpy as np

# Suppress Windows symlink warning that can cause cache allocation failures
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from src import config
from src.utils import get_logger

logger = get_logger("embeddings", config.SCRAPE_LOG)

_MODEL = None   # lazy singleton


class OnnxEmbedder:
    """
    The embedding model exported to ONNX (scripts/export_onnx_embedder.py), run with
    onnxruntime + the model's own fast tokenizer. Reproduces sentence-transformers'
    pipeline for this model exactly — strip (+ lower-case when the model config says
    so) → tokenize (truncate to max_seq_length) → transformer → CLS or mean pooling →
    L2 normalisation — so query vectors match the vectors the index was built with.
    """

    def __init__(self, model_dir: str, threads: int = 1):
        import json as _json
        from pathlib import Path as _Path

        import onnxruntime as ort
        from tokenizers import Tokenizer

        d = _Path(model_dir)
        self.cfg = _json.loads((d / "embedder.json").read_text(encoding="utf-8"))
        self.tokenizer = Tokenizer.from_file(str(d / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=int(self.cfg.get("max_seq_length", 512)))
        pad_id = int(self.cfg.get("pad_token_id", 0))
        self.tokenizer.enable_padding(pad_id=pad_id, pad_token=self.cfg.get("pad_token", "[PAD]"))
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, threads)
        opts.inter_op_num_threads = 1
        opts.enable_cpu_mem_arena = False          # keep the resident footprint small
        if (d / "model.onnx.data").exists():
            # Pre-optimised model with weights in an external file (the Docker build
            # writes it): onnxruntime memory-maps the weights, and with no further graph
            # optimisation or weight prepacking they stay file-backed pages instead of
            # ~130 MB of process heap.
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
            opts.add_session_config_entry("session.disable_prepacking", "1")
        self.session = ort.InferenceSession(str(d / "model.onnx"), sess_options=opts,
                                            providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def encode(self, texts, batch_size: int = 32, **_ignored) -> np.ndarray:
        out = []
        prep = [str(t).strip() for t in texts]
        if self.cfg.get("lowercase"):
            prep = [t.lower() for t in prep]
        for i in range(0, len(prep), batch_size):
            enc = self.tokenizer.encode_batch(prep[i:i + batch_size])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
            hidden = self.session.run(None, feed)[0]                 # (batch, seq, dim)
            if self.cfg.get("pooling", "cls") == "cls":
                vec = hidden[:, 0, :]
            else:                                                     # mean over real tokens
                m = mask[..., None].astype(hidden.dtype)
                vec = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
            if self.cfg.get("normalize", True):
                vec = vec / np.clip(np.linalg.norm(vec, axis=1, keepdims=True), 1e-12, None)
            out.append(vec.astype(np.float32))
        dim = int(self.cfg.get("dim", 384))
        return np.vstack(out) if out else np.zeros((0, dim), dtype=np.float32)


def _get_model():
    global _MODEL
    if _MODEL is None and config.EMBEDDING_BACKEND == "onnx":
        if not config.EMBEDDING_ONNX_DIR:
            raise RuntimeError("EMBEDDING_BACKEND=onnx needs EMBEDDING_ONNX_DIR (see scripts/export_onnx_embedder.py).")
        logger.info(f"Loading ONNX embedding model from {config.EMBEDDING_ONNX_DIR}")
        _MODEL = OnnxEmbedder(config.EMBEDDING_ONNX_DIR, threads=config.EMBEDDING_THREADS)
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer

        logger.info(f"Loading embedding model: {config.EMBEDDING_MODEL}")

        last_exc = None
        for attempt in range(1, 4):
            try:
                # After a failed online attempt, use the local cache only (a cached
                # model must not become unusable because the Hub is unreachable).
                _MODEL = SentenceTransformer(config.EMBEDDING_MODEL,
                                             local_files_only=attempt > 1)
                logger.info("Embedding model loaded successfully")
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    f"Model load attempt {attempt} failed: {exc}. "
                    + ("Retrying…" if attempt < 3 else "Giving up.")
                )

        if _MODEL is None:
            raise RuntimeError(
                f"\n\n❌  Could not load embedding model '{config.EMBEDDING_MODEL}' "
                f"after 3 attempts.\n"
                f"Last error: {last_exc}\n\n"
                "── Fixes ──────────────────────────────────────────────────\n"
                "1. Delete the broken cache folder and retry:\n"
                "   C:\\Users\\<you>\\.cache\\huggingface\\hub\\"
                "models--BAAI--bge-small-en-v1.5\\\n\n"
                "2. Or switch to the lighter 22 MB model — edit your .env:\n"
                "   EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2\n\n"
                "3. Or run VS Code / terminal as Administrator to allow\n"
                "   Windows symlinks (needed for HF cache on some machines).\n"
            )
    return _MODEL


def embed_texts(texts: List[str], batch_size: int = 64, show_progress: bool = False) -> np.ndarray:
    """
    Embed a list of texts.
    Returns normalised float32 numpy array of shape (N, DIM).
    """
    model = _get_model()
    if isinstance(model, OnnxEmbedder):
        return model.encode(texts, batch_size=min(batch_size, 32))
    embeddings = model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=show_progress,
        normalize_embeddings=True,   # cosine via inner-product
        convert_to_numpy=True,
    )
    return embeddings.astype(np.float32)


def embed_query(query: str) -> np.ndarray:
    """Embed a single query string; return shape (1, DIM) float32."""
    return embed_texts([query])
