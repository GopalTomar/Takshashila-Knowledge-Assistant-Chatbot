"""Mattermost callback signing, encrypted KB bundles, frontend build, repo hygiene."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


# ── Mattermost: unsigned callbacks are rejected ────────────────────────────────────
@pytest.fixture
def mm(monkeypatch):
    import integrations.mattermost_bot as bot
    monkeypatch.setattr(bot, "_ACTION_SECRET", b"test-secret")
    monkeypatch.setattr(bot, "MATTERMOST_SLASH_TOKEN", "slash-token")
    monkeypatch.setattr(bot, "READINESS_PROBE", None)      # standalone bot: no hosting API probe
    return bot, TestClient(bot.app)


def test_action_without_signature_rejected(mm):
    bot, c = mm
    r = c.post("/mattermost/action", json={"context": {"action": "delete_all_confirm"}, "channel_id": "victim"})
    assert r.status_code == 403


def test_action_with_forged_or_tampered_signature_rejected(mm):
    bot, c = mm
    ctx = bot._action("x", "feedback", verdict="helpful", question="q")["integration"]["context"]
    tampered = dict(ctx, question="something else")
    assert c.post("/mattermost/action", json={"context": tampered}).status_code == 403
    assert c.post("/mattermost/action", json={"context": dict(ctx, sig="0" * 40)}).status_code == 403


def test_signed_action_accepted(mm, monkeypatch):
    bot, c = mm
    monkeypatch.setattr(bot, "record_feedback", lambda **kw: None)
    ctx = bot._action("👍", "feedback", verdict="helpful", question="q")["integration"]["context"]
    r = c.post("/mattermost/action", json={"context": ctx, "post_id": "p1"})
    assert r.status_code == 200 and "ephemeral_text" in r.json()


def test_dialog_state_must_be_signed_and_channel_bound(mm):
    bot, c = mm
    forged = {"callback_id": "user", "submission": {"target": "someone"}, "channel_id": "c1",
              "state": json.dumps({"p": "post", "q": "leak me", "c": "c1"})}
    assert "expired" in c.post("/mattermost/dialog", json=forged).json()["errors"]["target"]
    good_state = bot._signed_state({"p": "post", "q": "q", "c": "c1"})
    other_channel = dict(forged, state=json.dumps(good_state), channel_id="c2")
    assert "expired" in c.post("/mattermost/dialog", json=other_channel).json()["errors"]["target"]


def test_slash_token_checked(mm):
    bot, c = mm
    assert c.post("/mattermost/ask", data={"token": "nope", "text": "hi"}).status_code == 403
    assert c.post("/mattermost/ask", data={"token": "slash-token", "text": ""}).status_code == 200


# ── Mattermost: contexts bound to a channel; guests never get internal answers ──────
def test_action_context_is_channel_bound(mm):
    bot, c = mm
    unbound = bot._action("x", "delete_cancel", question="q")["integration"]["context"]
    assert c.post("/mattermost/action", json={"context": unbound, "channel_id": "c1"}).status_code == 403
    props = bot._bind_props({"attachments": [{"actions": [bot._action("x", "delete_cancel", question="q")]}]}, "c1")
    ctx = props["attachments"][0]["actions"][0]["integration"]["context"]
    assert ctx["cid"] == "c1" and bot._verify_payload(dict(ctx))
    # Replayed from another channel → refused; from its own channel → accepted.
    assert c.post("/mattermost/action", json={"context": ctx, "channel_id": "c2"}).status_code == 403
    assert c.post("/mattermost/action", json={"context": ctx, "channel_id": "c1"}).status_code != 403
    # Tampering with the binding breaks the signature.
    assert c.post("/mattermost/action", json={"context": dict(ctx, cid="c2"), "channel_id": "c2"}).status_code == 403


def _mm_api(monkeypatch, *, guest=False, member=True, guests_in_channel=0):
    from integrations import mattermost_api as api
    monkeypatch.setattr(api, "is_configured", lambda: True)
    monkeypatch.setattr(api, "is_guest", lambda uid: guest)
    monkeypatch.setattr(api, "user_in_channel", lambda cid, uid: member)
    monkeypatch.setattr(api, "channel_guest_count", lambda cid: guests_in_channel)
    monkeypatch.setattr(api, "bot_in_channel", lambda cid: True)
    monkeypatch.setattr(api, "find_channel_on_team", lambda t, n: {"id": "c9", "name": n})
    return api


@pytest.mark.parametrize("guest", [True, None])        # None = role lookup failed → fail closed
def test_guest_or_unverifiable_requester_denied(mm, monkeypatch, guest):
    bot, c = mm
    _mm_api(monkeypatch, guest=guest)
    ran = []
    monkeypatch.setattr(bot, "run_rag_and_reply", lambda **kw: ran.append(kw))
    r = c.post("/mattermost/ask", data={"token": "slash-token", "text": "internal question",
                                        "user_id": "g1", "channel_id": "c1"})
    assert r.status_code == 200 and not ran
    assert "staff" in r.json()["text"] or "verify" in r.json()["text"]


def test_guest_clicker_cannot_trigger_actions(mm, monkeypatch):
    bot, c = mm
    _mm_api(monkeypatch, guest=True)
    props = bot._bind_props({"attachments": [{"actions": [bot._action("x", "related", question="q")]}]}, "c1")
    ctx = props["attachments"][0]["actions"][0]["integration"]["context"]
    r = c.post("/mattermost/action", json={"context": ctx, "channel_id": "c1", "user_id": "g1"})
    assert "staff accounts only" in r.json()["ephemeral_text"]


def _channel_send(monkeypatch, **kw):
    from integrations.command_parser import Destination
    from integrations.destination_handlers import channel_handler
    from integrations.destination_handlers.base import Requester, ResponsePayload
    _mm_api(monkeypatch, **kw)
    posted = []
    monkeypatch.setattr(channel_handler, "deliver", lambda cid, p, header="": posted.append(cid) or "post1")
    res = channel_handler.send_to_channel(
        Destination(kind="channel", channel_name="research"),
        ResponsePayload(question="q", message="internal answer"),
        Requester(user_id="u1", user_name="u", channel_id="c1", team_id="t1"))
    return res, posted


def test_channel_routing_requires_membership_and_no_guests(monkeypatch):
    res, posted = _channel_send(monkeypatch, member=False)
    assert not res.ok and "not a member" in res.error and not posted
    res, posted = _channel_send(monkeypatch, guests_in_channel=2)
    assert not res.ok and "guest" in res.error and not posted
    res, posted = _channel_send(monkeypatch, guests_in_channel=None)
    assert not res.ok and not posted
    res, posted = _channel_send(monkeypatch)
    assert res.ok and posted == ["c9"]


def test_user_routing_refuses_guest_recipient(monkeypatch):
    from integrations.command_parser import Destination
    from integrations.destination_handlers import user_handler
    from integrations.destination_handlers.base import Requester, ResponsePayload
    api = _mm_api(monkeypatch)
    monkeypatch.setattr(api, "find_user_by_username", lambda n: {"id": "x1", "username": n, "roles": "system_guest"})
    monkeypatch.setattr(api, "get_or_create_dm_channel", lambda uid: pytest.fail("DM must not be opened"))
    res = user_handler.send_to_user_dm(Destination(kind="user", usernames=("ext",)),
                                       ResponsePayload(question="q", message="a"),
                                       Requester(user_id="u1", user_name="u", channel_id="c1"))
    assert not res.ok and "guest" in res.error


def test_public_answer_downgraded_in_channel_with_guests(mm, monkeypatch):
    bot, c = mm
    _mm_api(monkeypatch, guests_in_channel=1)
    ran = []
    monkeypatch.setattr(bot, "run_rag_and_reply", lambda **kw: ran.append(kw))
    c.post("/mattermost/ask", data={"token": "slash-token", "text": "public what is the red flag rule",
                                    "user_id": "u1", "channel_id": "c1"})
    assert ran and ran[0]["visibility"] == "private"



@pytest.mark.parametrize("action", ["export_markdown", "export_pdf", "related"])
def test_channel_visible_actions_refused_where_guests_are(mm, monkeypatch, action):
    bot, c = mm
    _mm_api(monkeypatch, guests_in_channel=3)
    monkeypatch.setattr(bot, "MATTERMOST_BOT_TOKEN", "t")
    monkeypatch.setattr(bot, "MATTERMOST_URL", "https://mm.example.org")
    monkeypatch.setattr(bot, "_related_task", lambda *a: pytest.fail("must not post"))
    monkeypatch.setattr(bot, "_export_markdown_task", lambda *a: pytest.fail("must not post"))
    props = bot._bind_props({"attachments": [{"actions": [bot._action("x", action, question="q")]}]}, "c1")
    ctx = props["attachments"][0]["actions"][0]["integration"]["context"]
    r = c.post("/mattermost/action", json={"context": ctx, "channel_id": "c1", "user_id": "u1"})
    assert "guest" in r.json()["ephemeral_text"]


def test_askkb_while_kb_loading_gets_a_wake_up_message(mm, monkeypatch):
    bot, c = mm
    _mm_api(monkeypatch)
    ran = []
    monkeypatch.setattr(bot, "run_rag_and_reply", lambda **kw: ran.append(kw))
    monkeypatch.setattr(bot, "READINESS_PROBE", lambda: False)
    r = c.post("/mattermost/ask", data={"token": "slash-token", "text": "what is the red flag rule",
                                        "user_id": "u1", "channel_id": "c1"})
    assert r.status_code == 200 and "starting up" in r.json()["text"] and not ran
    monkeypatch.setattr(bot, "READINESS_PROBE", lambda: True)
    c.post("/mattermost/ask", data={"token": "slash-token", "text": "what is the red flag rule",
                                    "user_id": "u1", "channel_id": "c1"})
    assert ran

# ── Encrypted KB bundle ────────────────────────────────────────────────────────────
def _mk_root(tmp_path):
    root = tmp_path / "rel"
    (root / "index").mkdir(parents=True)
    (root / "processed").mkdir()
    (root / "index" / "faiss.index").write_bytes(b"x" * 5_000_000)     # spans >1 block
    (root / "processed" / "documents.jsonl").write_text('{"a": 1}\n')
    (root / "kb_manifest.json").write_text(json.dumps({"version": "v1"}))
    return root


def test_bundle_roundtrip(tmp_path):
    from src import kb_bundle
    key = kb_bundle.generate_key()
    man = kb_bundle.pack(_mk_root(tmp_path), tmp_path / "b.tkkb", key)
    assert man["version"] == "v1" and len(man["sha256"]) == 64
    out = kb_bundle.unpack(tmp_path / "b.tkkb", tmp_path / "out", key, man["sha256"])
    assert (out / "index" / "faiss.index").stat().st_size == 5_000_000
    assert b"documents" not in (tmp_path / "b.tkkb").read_bytes()          # encrypted, not plain tar



def test_bundle_is_uncompressed_and_older_gzip_bundles_still_unpack(tmp_path):
    import tarfile as _tar
    from src import kb_bundle
    key = kb_bundle.generate_key()
    root = _mk_root(tmp_path)
    kb_bundle.pack(root, tmp_path / "new.tkkb", key)
    plain = tmp_path / "plain.tar"
    with open(tmp_path / "new.tkkb", "rb") as f, open(plain, "wb") as o:
        kb_bundle.decrypt_stream(f, o, key)
    with _tar.open(plain, mode="r:") as t:                         # plain tar (fast to unpack)
        assert "index/faiss.index" in t.getnames()
    # a bundle produced by the previous (gzip) format
    gz = io.BytesIO()
    with _tar.open(fileobj=gz, mode="w:gz") as t:
        for m in ("index", "processed", "kb_manifest.json"):
            t.add(str(root / m), arcname=m)
    gz.seek(0)
    with open(tmp_path / "old.tkkb", "wb") as o:
        kb_bundle.encrypt_stream(gz, o, key)
    out = kb_bundle.unpack(tmp_path / "old.tkkb", tmp_path / "old_out", key)
    assert (out / "index" / "faiss.index").stat().st_size == 5_000_000

def test_bundle_wrong_key_tamper_and_truncation_fail(tmp_path):
    from src import kb_bundle
    key = kb_bundle.generate_key()
    kb_bundle.pack(_mk_root(tmp_path), tmp_path / "b.tkkb", key)
    data = (tmp_path / "b.tkkb").read_bytes()
    with pytest.raises(Exception):
        kb_bundle.unpack(tmp_path / "b.tkkb", tmp_path / "o1", kb_bundle.generate_key())
    bad = bytearray(data)
    bad[100] ^= 0xFF
    (tmp_path / "t.tkkb").write_bytes(bytes(bad))
    with pytest.raises(Exception):
        kb_bundle.unpack(tmp_path / "t.tkkb", tmp_path / "o2", key)
    (tmp_path / "s.tkkb").write_bytes(data[: len(data) // 2])              # drop final block
    with pytest.raises(Exception):
        kb_bundle.unpack(tmp_path / "s.tkkb", tmp_path / "o3", key)
    with pytest.raises(ValueError):
        kb_bundle.unpack(tmp_path / "b.tkkb", tmp_path / "o4", key, expected_sha256="0" * 64)


def test_bundle_rejects_path_traversal(tmp_path):
    from src import kb_bundle
    key = kb_bundle.generate_key()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"pwned"
        ti = tarfile.TarInfo("../escape.txt")
        ti.size = len(data)
        tar.addfile(ti, io.BytesIO(data))
    buf.seek(0)
    with open(tmp_path / "evil.tkkb", "wb") as f:
        kb_bundle.encrypt_stream(buf, f, key)
    with pytest.raises(ValueError):
        kb_bundle.unpack(tmp_path / "evil.tkkb", tmp_path / "dest", key)
    assert not (tmp_path / "escape.txt").exists()


# ── Frontend build ─────────────────────────────────────────────────────────────────
def _build(tmp_path, api):
    import os
    env = dict(os.environ, API_BASE_URL=api)
    return subprocess.run([sys.executable, str(ROOT / "frontend" / "build.py"), "--out", str(tmp_path / "site")],
                          env=env, capture_output=True, text=True)


def test_frontend_build_injects_only_api_url(tmp_path):
    r = _build(tmp_path, "https://api.example.org")
    assert r.returncode == 0, r.stderr
    site = tmp_path / "site"
    assert 'apiBaseUrl": "https://api.example.org"' in (site / "config.js").read_text()
    for f in ("index.html", "app.js", "styles.css", "assets/logo-dark.svg", ".nojekyll"):
        assert (site / f).exists()


def test_frontend_build_rejects_insecure_or_missing_url(tmp_path):
    assert _build(tmp_path, "http://api.example.org").returncode == 2
    assert _build(tmp_path, "").returncode == 2


def test_frontend_escapes_untrusted_text():
    js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert "function esc(" in js and "safeUrl" in js
    assert "innerHTML = renderAnswer(" in js            # the only innerHTML sink; input is escaped first


# ── Repository hygiene ─────────────────────────────────────────────────────────────
def test_gitignore_and_dockerignore_protect_secrets():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    di = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for pat in (".env", "data/processed/", "data/index/", "commit_kb_clean_crawled/", "data/releases/"):
        assert pat in gi
    assert ".env" in di and "data/" in di
    assert "COPY . ." not in (ROOT / "Dockerfile").read_text()


def test_no_secrets_in_tracked_files():
    import re
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    pat = re.compile(r"(gsk_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{30,}|ghp_[A-Za-z0-9]{30,}|xox[bp]-[A-Za-z0-9-]{20,})")
    hits = []
    for f in files:
        p = ROOT / f
        if p.suffix in (".py", ".md", ".txt", ".yml", ".yaml", ".json", ".js", ".html", ".toml", ".example") \
                and p.exists() and p.stat().st_size < 2_000_000:
            if pat.search(p.read_text(encoding="utf-8", errors="ignore")):
                hits.append(f)
    assert hits == []


def test_no_pickle_loading_by_default():
    src = (ROOT / "src" / "vector_store.py").read_text(encoding="utf-8")
    assert "ALLOW_PICKLE_METADATA" in src
    from src import config
    assert config.ALLOW_PICKLE_METADATA is False
