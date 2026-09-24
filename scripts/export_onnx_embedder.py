#!/usr/bin/env python3
"""
export_onnx_embedder.py — Export the embedding model to ONNX for the torch-free API.

    python scripts/export_onnx_embedder.py --out models/embedder [--model BAAI/bge-small-en-v1.5]

Writes ``model.onnx`` + ``model.onnx.data`` (fp32, same weights, graph pre-optimised
by onnxruntime; weights in the external file so the API can memory-map them),
``tokenizer.json`` and ``embedder.json``
(pooling / normalisation / max length, read from the model's sentence-transformers
config). Then verifies parity: the ONNX pipeline (src.embeddings.OnnxEmbedder) must
reproduce sentence-transformers' vectors (cosine >= 0.9999 on sample texts, including
long truncated ones) or the script fails — the index is built with
sentence-transformers, so the API's query vectors must match it.

Used by the Dockerfile's build stage; needs torch + sentence-transformers + onnx
(build-time only — the runtime image has neither torch nor sentence-transformers).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SAMPLES = [
    "What is Takshashila Institution?",
    "What has Takshashila published about geospatial technology and remote sensing?",
    "Pranay Kotasthane op-eds on semiconductors",
    "  Leading and trailing spaces, MIXED Case, punctuation!?  ",
    "नमस्ते — unicode text, émigré, naïve café",
    "India's high-tech geopolitics " * 200,          # > 512 tokens: exercises truncation
]


def export(model_name: str, out: Path) -> None:
    import torch
    from sentence_transformers import SentenceTransformer

    st = SentenceTransformer(model_name, device="cpu")
    transformer = st[0]
    pooling = st[1] if len(st) > 1 else None
    hf_model = transformer.auto_model.eval()
    tok = transformer.tokenizer

    out.mkdir(parents=True, exist_ok=True)
    enc = tok(["export sample"], return_tensors="pt")
    names = [n for n in ("input_ids", "attention_mask", "token_type_ids") if n in enc]

    class Wrapper(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, *args):
            return self.m(**dict(zip(names, args))).last_hidden_state

    dyn = {n: {0: "batch", 1: "seq"} for n in names}
    dyn["last_hidden_state"] = {0: "batch", 1: "seq"}
    args = tuple(enc[n] for n in names)
    kw = dict(input_names=names, output_names=["last_hidden_state"], dynamic_axes=dyn,
              opset_version=17, do_constant_folding=True)
    raw = out / "_raw.onnx"
    try:
        torch.onnx.export(Wrapper(hf_model), args, str(raw), dynamo=False, **kw)
    except TypeError:                                   # older torch: no ``dynamo`` argument
        torch.onnx.export(Wrapper(hf_model), args, str(raw), **kw)
    _optimise_external(raw, out)

    tok.save_pretrained(str(out / "_tok"))
    shutil.copy2(out / "_tok" / "tokenizer.json", out / "tokenizer.json")
    shutil.rmtree(out / "_tok", ignore_errors=True)

    pool_mode = "cls"
    if pooling is not None and hasattr(pooling, "get_pooling_mode_str"):
        pool_mode = "cls" if pooling.get_pooling_mode_str() == "cls" else "mean"
    normalize = any(type(m).__name__ == "Normalize" for m in st)
    cfg = {
        "model": model_name,
        "dim": int(st.get_sentence_embedding_dimension()),
        "max_seq_length": int(st.max_seq_length),
        "lowercase": bool(getattr(transformer, "do_lower_case", False)),
        "pooling": pool_mode,
        "normalize": normalize,
        "pad_token": tok.pad_token or "[PAD]",
        "pad_token_id": int(tok.pad_token_id or 0),
    }
    (out / "embedder.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"Exported {model_name} → {out} {cfg}")


def _optimise_external(raw: Path, out: Path) -> None:
    """
    Let onnxruntime optimise the graph once, here, and write it with ALL weights in
    ``model.onnx.data``. At runtime the API loads it with optimisation and weight
    prepacking disabled, so onnxruntime memory-maps the weights (file-backed pages,
    not process heap) — the difference between fitting 512 MB or not.
    """
    import onnxruntime as ort
    for f in ("model.onnx", "model.onnx.data"):
        (out / f).unlink(missing_ok=True)
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
    so.optimized_model_filepath = str(out / "model.onnx")
    so.add_session_config_entry("session.optimized_model_external_initializers_file_name", "model.onnx.data")
    so.add_session_config_entry("session.optimized_model_external_initializers_min_size_in_bytes", "1024")
    ort.InferenceSession(str(raw), so, providers=["CPUExecutionProvider"])
    raw.unlink(missing_ok=True)
    for extra in out.glob("_raw.onnx*"):
        extra.unlink()


def verify(model_name: str, out: Path, min_cos: float = 0.9999) -> float:
    from sentence_transformers import SentenceTransformer

    from src.embeddings import OnnxEmbedder

    ref = SentenceTransformer(model_name, device="cpu").encode(
        SAMPLES, normalize_embeddings=True, convert_to_numpy=True)
    got = OnnxEmbedder(str(out)).encode(SAMPLES)
    cos = float(np.min(np.sum(ref * got, axis=1)))
    print(f"Parity: min cosine {cos:.7f}, max |diff| {float(np.max(np.abs(ref - got))):.2e}")
    if cos < min_cos:
        raise SystemExit(f"ONNX embedder does not match sentence-transformers (min cosine {cos} < {min_cos})")
    return cos


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="BAAI/bge-small-en-v1.5")
    ap.add_argument("--out", default="models/embedder")
    ap.add_argument("--skip-verify", action="store_true")
    a = ap.parse_args(argv)
    out = Path(a.out)
    export(a.model, out)
    if not a.skip_verify:
        verify(a.model, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
