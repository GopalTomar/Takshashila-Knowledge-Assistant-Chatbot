"""
kb_bundle.py — Pack / encrypt / verify / unpack a KB release for distribution.

The refresh job (GitHub Actions) builds a release and publishes it as a GitHub
Release asset; the API (Railway) downloads and activates it. Because the GitHub
repository is PUBLIC and the KB contains internal Commit KB content, the bundle
is always encrypted:

    bundle file  = MAGIC(8) | for each ≤4 MiB block: nonce(12) | len(4) | AESGCM(block)
    AAD per block = MAGIC | block index (8 bytes) | final flag (1 byte)
    key           = KB_BUNDLE_KEY: 32 random bytes, urlsafe-base64 (generate_key())

Binding the block index and a final flag into the authenticated data means a
truncated, reordered or spliced file fails to decrypt instead of silently
yielding a partial KB. The manifest (public, unencrypted) holds only version,
size and sha256 of the encrypted file — no content.

Extraction refuses absolute paths, ``..`` components and links (tar-slip safe).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable

MAGIC = b"TKKBv1\x00\x00"
BLOCK = 4 * 1024 * 1024
BUNDLE_MEMBERS = ("processed", "index", "state", "reports", "kb_manifest.json")


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _key(key_b64: str) -> bytes:
    if not key_b64:
        raise ValueError("KB_BUNDLE_KEY is not set.")
    raw = base64.urlsafe_b64decode(key_b64.strip() + "=" * (-len(key_b64.strip()) % 4))
    if len(raw) != 32:
        raise ValueError("KB_BUNDLE_KEY must decode to 32 bytes (use kb_bundle.generate_key()).")
    return raw


def _aad(index: int, final: bool) -> bytes:
    return MAGIC + struct.pack(">Q", index) + (b"\x01" if final else b"\x00")


def encrypt_stream(src, dst, key_b64: str) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes = AESGCM(_key(key_b64))
    dst.write(MAGIC)
    idx = 0
    block = src.read(BLOCK)
    while True:
        nxt = src.read(BLOCK)
        final = not nxt
        nonce = os.urandom(12)
        ct = aes.encrypt(nonce, block, _aad(idx, final))
        dst.write(nonce + struct.pack(">I", len(ct)) + ct)
        if final:
            break
        block, idx = nxt, idx + 1


def decrypt_stream(src, dst, key_b64: str) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes = AESGCM(_key(key_b64))
    if src.read(len(MAGIC)) != MAGIC:
        raise ValueError("Not a KB bundle (bad magic).")
    idx = 0
    header = src.read(16)
    while header:
        if len(header) != 16:
            raise ValueError("Truncated bundle.")
        nonce, (n,) = header[:12], struct.unpack(">I", header[12:])
        ct = src.read(n)
        if len(ct) != n:
            raise ValueError("Truncated bundle.")
        nxt = src.read(16)
        final = not nxt
        dst.write(aes.decrypt(nonce, ct, _aad(idx, final)))   # raises InvalidTag if tampered
        header, idx = nxt, idx + 1


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pack(root: Path, out_file: Path, key_b64: str, members: Iterable[str] = BUNDLE_MEMBERS) -> Dict:
    """Tar+gzip the KB members of ``root`` and encrypt to ``out_file``. Returns a manifest."""
    root = Path(root)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as tmp:
        with tarfile.open(fileobj=tmp, mode="w:gz") as tar:
            for m in members:
                p = root / m
                if p.exists():
                    tar.add(str(p), arcname=m,
                            filter=lambda ti: None if ti.name.endswith((".tmp", ".pkl")) else ti)
        tmp.seek(0)
        with open(out_file, "wb") as f:
            encrypt_stream(tmp, f, key_b64)
    kb_manifest = {}
    if (root / "kb_manifest.json").exists():
        kb_manifest = json.loads((root / "kb_manifest.json").read_text(encoding="utf-8"))
    return {
        "format": "tkkb-v1",
        "version": kb_manifest.get("version") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "asset": out_file.name,
        "size": out_file.stat().st_size,
        "sha256": sha256_file(out_file),
        "embedding_model": kb_manifest.get("embedding_model", ""),
    }


def _safe_members(tar: tarfile.TarFile):
    for m in tar.getmembers():
        name = m.name
        if name.startswith(("/", "\\")) or ".." in Path(name).parts or ":" in name:
            raise ValueError(f"Unsafe path in bundle: {name!r}")
        if m.issym() or m.islnk() or m.isdev():
            raise ValueError(f"Links/devices not allowed in bundle: {name!r}")
        if name.split("/")[0] not in BUNDLE_MEMBERS:
            raise ValueError(f"Unexpected bundle member: {name!r}")
        yield m


def unpack(bundle_file: Path, dest: Path, key_b64: str, expected_sha256: str = "") -> Path:
    """Verify, decrypt and extract a bundle into ``dest`` (created). Returns ``dest``."""
    if expected_sha256 and sha256_file(bundle_file) != expected_sha256:
        raise ValueError("Bundle checksum mismatch — refusing to activate.")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as tmp:
        with open(bundle_file, "rb") as f:
            decrypt_stream(f, tmp, key_b64)
        tmp.seek(0)
        with tarfile.open(fileobj=tmp, mode="r:gz") as tar:
            members = list(_safe_members(tar))
            tar.extractall(str(dest), members=members)
    return dest


def main(argv=None) -> int:
    """CLI: python -m src.kb_bundle {genkey|pack|unpack} ..."""
    import argparse
    ap = argparse.ArgumentParser(prog="python -m src.kb_bundle")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("genkey")
    p = sub.add_parser("pack")
    p.add_argument("--root", default="")
    p.add_argument("--out", required=True)
    p.add_argument("--manifest", required=True)
    u = sub.add_parser("unpack")
    u.add_argument("--bundle", required=True)
    u.add_argument("--dest", required=True)
    u.add_argument("--sha256", default="")
    args = ap.parse_args(argv)
    if args.cmd == "genkey":
        print(generate_key())
        return 0
    from src import config
    key = os.getenv("KB_BUNDLE_KEY", "")
    if args.cmd == "pack":
        root = Path(args.root) if args.root else config.resolve_active_kb_root()
        man = pack(root, Path(args.out), key)
        Path(args.manifest).write_text(json.dumps(man, indent=2), encoding="utf-8")
        print(json.dumps(man, indent=2))
        return 0
    unpack(Path(args.bundle), Path(args.dest), key, args.sha256)
    print(f"extracted to {args.dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
