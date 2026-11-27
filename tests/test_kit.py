import pytest

from conftest import add_ad
from jobalerts import config, kit, telegram, triage

KIT = {"deadline": "15 December", "contact": "Kari, kari@acme.no", "how_to_apply": "Apply through the form.",
       "requirements": ["B2B sales", "English"], "gaps": ["3 years of SaaS"], "talking_points": ["Sold to SMEs <5>"],
       "cover_letter": "Dear Kari,\nI am applying..."}


@pytest.fixture
def sent(monkeypatch, tmp_path):
    profile = tmp_path / "profile.md"
    profile.write_text("B2B sales in Oslo.", encoding="utf-8")
    monkeypatch.setattr(config, "PROFILE_PATH", profile)
    calls = []

    def fake_api(method, **params):
        calls.append((method, params))
        return {"message_id": 99}
    monkeypatch.setattr(telegram, "api", fake_api)
    return calls


def decide(con, uuid, decision, n):
    con.execute("INSERT INTO decisions(uuid, decision, decided_at, telegram_update_id) VALUES(?,?,?,?)",
                (uuid, decision, "x", n))
    con.commit()


def test_only_the_latest_keep_gets_a_kit(con):
    for uuid in ("kept", "changed", "rejected"):
        add_ad(con, uuid)
    decide(con, "kept", "keep", 1)
    decide(con, "changed", "keep", 2)
    decide(con, "changed", "reject", 3)
    decide(con, "rejected", "reject", 4)
    assert [r["uuid"] for r in kit.pending(con)] == ["kept"]


def test_kit_is_sent_once_as_reply(con, sent, monkeypatch):
    prompts = []
    monkeypatch.setattr(triage, "ai_json", lambda prompt, schema: prompts.append(prompt) or KIT)
    add_ad(con, "a", application_url="https://acme.no/apply")
    con.execute("INSERT INTO sent_messages(message_id, uuid, sent_at) VALUES(7, 'a', 'x')")
    decide(con, "a", "keep", 1)
    assert kit.send_kits(con) == 1
    assert "B2B sales in Oslo." in prompts[0] and "Sell B2B software." in prompts[0]
    (m1, p1), (m2, p2) = sent
    assert p1["reply_parameters"]["message_id"] == 7 and "reply_parameters" not in p2
    assert "15 December" in p1["text"] and "https://acme.no/apply" in p1["text"] and "&lt;5&gt;" in p1["text"]
    assert "<code>Dear Kari," in p2["text"]
    assert kit.send_kits(con) == 0 and len(sent) == 2  # never twice


def test_kit_retries_then_reports(con, sent, monkeypatch):
    def boom(prompt, schema):
        raise RuntimeError("AI down")
    monkeypatch.setattr(triage, "ai_json", boom)
    add_ad(con, "a")
    decide(con, "a", "keep", 1)
    for _ in range(kit.MAX_ATTEMPTS):
        assert kit.send_kits(con) == 0
    assert len(sent) == 1 and "could not be prepared" in sent[0][1]["text"]
    assert kit.pending(con) == []


def test_telegram_failure_keeps_the_ai_answer(con, sent, monkeypatch):
    answers = []
    monkeypatch.setattr(triage, "ai_json", lambda p, s: answers.append(1) or KIT)
    add_ad(con, "a")
    decide(con, "a", "keep", 1)

    def down(method, **params):
        raise RuntimeError("Telegram down")
    monkeypatch.setattr(telegram, "api", down)
    assert kit.send_kits(con) == 0
    monkeypatch.setattr(telegram, "api", lambda method, **p: sent.append((method, p)) or {})
    assert kit.send_kits(con) == 1 and len(answers) == 1


def test_missing_update_numbers_send_a_warning(con, monkeypatch):
    from jobalerts import db
    db.set_meta(con, "tg_offset", "100")
    batches = [[{"update_id": 103, "message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "hi"}}], []]
    texts = []

    def fake_api(method, **params):
        if method == "getUpdates":
            return batches.pop(0)
        texts.append(params.get("text"))
        return {}
    monkeypatch.setattr(telegram, "api", fake_api)
    stats = telegram.collect_updates(con)
    assert stats["lost"] == 3 and "3 Telegram click(s)" in texts[0]


def test_run_module_imports():
    import importlib
    importlib.import_module("jobalerts.run")
