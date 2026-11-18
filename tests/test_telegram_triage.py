import pytest
from conftest import add_ad

from jobalerts import config, db, telegram, texts, triage


def test_texts_have_same_keys():
    assert set(texts.TEXTS["en"]) == set(texts.TEXTS["fr"])


def test_keyboard_marks_choice():
    kb = telegram.keyboard("abc", "reject")["inline_keyboard"][0]
    assert [b["text"] for b in kb] == ["Keep", "✔ Reject"]
    assert [b["callback_data"] for b in kb] == ["k:abc", "r:abc"]


def test_format_offer_escapes_html():
    ad = dict(title="Sales <Lead> & co", employer="A&B", municipal="OSLO", score=4,
              norwegian_required=1, source="finn", summary_fr="Line 1\nLine 2",
              url='https://example.org/?a=1&b="x"')
    text = telegram.format_offer(ad)
    assert "<b>Sales &lt;Lead&gt; &amp; co</b>" in text
    assert "A&amp;B · Oslo · 4/5" in text
    assert "Norwegian required" in text and "Finn: limited preview" in text
    assert 'href="https://example.org/?a=1&amp;b=&quot;x&quot;"' in text


def test_collect_updates_stores_clicks_and_replies(con, monkeypatch):
    add_ad(con, "u1")
    con.execute("INSERT INTO sent_messages(message_id, uuid, sent_at) VALUES(7, 'u1', 'x')")
    con.commit()
    owner = {"id": 42}
    batches = [[
        {"update_id": 100, "callback_query": {"from": owner, "data": "r:u1",
                                              "message": {"message_id": 7, "chat": owner}}},
        {"update_id": 101, "message": {"from": owner, "chat": owner, "text": "Too junior",
                                       "reply_to_message": {"message_id": 7}}},
        {"update_id": 102, "callback_query": {"from": {"id": 666}, "data": "k:u1",  # stranger
                                              "message": {"message_id": 7, "chat": {"id": 666}}}},
    ], []]
    edits = []

    def fake_api(method, **params):
        if method == "getUpdates":
            return batches.pop(0)
        edits.append((method, params["message_id"], params["reply_markup"]))
        return {}

    monkeypatch.setattr(telegram, "api", fake_api)
    stats = telegram.collect_updates(con)
    assert stats == dict(updates=3, decisions=1, notes=1, ignored=1)
    row = con.execute("SELECT decision, reason FROM decisions WHERE uuid='u1'").fetchone()
    assert (row["decision"], row["reason"]) == ("reject", "Too junior")
    assert db.get_meta(con, "tg_offset") == "103"
    assert edits and edits[0][2]["inline_keyboard"][0][1]["text"] == "✔ Reject"


def test_build_prompt_and_examples(con, tmp_path, monkeypatch):
    profile = tmp_path / "profile.md"
    profile.write_text("# Profile\nB2B sales, Oslo.", encoding="utf-8")
    monkeypatch.setattr(config, "PROFILE_PATH", profile)
    add_ad(con, "old", title="Telemarketer")
    con.execute("INSERT INTO decisions(uuid, decision, reason, decided_at) VALUES('old','reject','call center','x')")
    add_ad(con, "new", description="Great B2B role " + "x" * 5000)
    con.commit()
    ads = con.execute("SELECT * FROM ads WHERE uuid='new'").fetchall()
    prompt = triage.build_prompt(con, ads)
    assert "You help Alex sort job ads" in prompt
    assert "exactly 2 lines in English" in prompt
    assert "B2B sales, Oslo." in prompt
    assert "- Telemarketer (Acme AS) | reason: call center" in prompt
    assert "## Ad id=1" in prompt
    assert len(prompt) < 9000  # description truncated to MAX_DESC_CHARS


def test_triage_stores_results_and_retries_missing(con, tmp_path, monkeypatch):
    profile = tmp_path / "profile.md"
    profile.write_text("profile", encoding="utf-8")
    monkeypatch.setattr(config, "PROFILE_PATH", profile)
    add_ad(con, "a")
    add_ad(con, "b")
    monkeypatch.setattr(triage, "call_ai", lambda prompt: [
        dict(id=1, relevant=True, score=9, summary="Good.\nVery.", norwegian_required=False, reason="fit"),
    ])
    assert triage.triage(con) == 1
    a = con.execute("SELECT * FROM ads WHERE uuid='a'").fetchone()
    assert (a["relevant"], a["score"], a["summary_fr"]) == (1, 5, "Good.\nVery.")  # score clamped to 5
    b = con.execute("SELECT triaged_at FROM ads WHERE uuid='b'").fetchone()
    assert b["triaged_at"] is None  # no result: retried at the next run


# ---------- Gemini ----------

class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.ok = 200 <= status < 300
        self.text = str(data)

    def json(self):
        return self._data


RESULTS = {"results": [dict(id=1, relevant=True, score=4, summary="a\nb", norwegian_required=False, reason="ok")]}


def _gemini_answer(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_call_ai_dispatches_on_provider(monkeypatch):
    monkeypatch.setattr(triage, "call_claude", lambda p: "claude")
    monkeypatch.setattr(triage, "call_gemini", lambda p: "gemini")
    monkeypatch.setattr(config, "AI_PROVIDER", "gemini")
    assert triage.call_ai("x") == "gemini"
    monkeypatch.setattr(config, "AI_PROVIDER", "claude")
    assert triage.call_ai("x") == "claude"


def test_call_gemini_sends_key_in_header_and_schema(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k-123")
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-flash-latest")
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append((url, headers, json))
        return FakeResponse(200, _gemini_answer('```json\n' + __import__("json").dumps(RESULTS) + '\n```'))

    monkeypatch.setattr(triage.requests, "post", fake_post)
    assert triage.call_gemini("prompt") == RESULTS["results"]
    url, headers, body = calls[0]
    assert url.endswith("/models/gemini-flash-latest:generateContent") and "k-123" not in url
    assert headers["x-goog-api-key"] == "k-123"
    assert body["generationConfig"]["responseFormat"]["text"]["schema"] == triage.SCHEMA
    assert body["contents"][0]["parts"][0]["text"] == "prompt"


def test_call_gemini_falls_back_to_older_fields(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    bodies = []

    def fake_post(url, headers, json, timeout):
        bodies.append(json["generationConfig"])
        if "responseFormat" in json["generationConfig"]:
            return FakeResponse(400, {"error": {"message": 'Unknown name "responseFormat"'}})
        return FakeResponse(200, _gemini_answer(__import__("json").dumps(RESULTS)))

    monkeypatch.setattr(triage.requests, "post", fake_post)
    assert triage.call_gemini("p") == RESULTS["results"]
    assert bodies[1] == {"responseMimeType": "application/json", "responseJsonSchema": triage.SCHEMA}


def test_call_gemini_errors_are_clear(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(triage.requests, "post", lambda *a, **kw: FakeResponse(
        400, {"error": {"message": "API key not valid. Please pass a valid API key."}}))
    with pytest.raises(RuntimeError, match="refused the API key"):
        triage.call_gemini("p")
    monkeypatch.setattr(triage.requests, "post", lambda *a, **kw: FakeResponse(
        200, {"promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(RuntimeError, match="no answer .SAFETY"):
        triage.call_gemini("p")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY missing"):
        triage.call_gemini("p")


def test_check_gemini(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(triage.requests, "get", lambda url, headers, timeout: FakeResponse(
        200, {"name": "models/gemini-flash-latest", "displayName": "Gemini Flash Latest"}))
    assert triage.check_gemini()["name"] == "Gemini Flash Latest"
