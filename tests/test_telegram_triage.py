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
    monkeypatch.setattr(triage, "call_claude", lambda prompt: [
        dict(id=1, relevant=True, score=5, summary="Good.\nVery.", norwegian_required=False, reason="fit"),
    ])
    assert triage.triage(con) == 1
    a = con.execute("SELECT * FROM ads WHERE uuid='a'").fetchone()
    assert (a["relevant"], a["score"], a["summary_fr"]) == (1, 5, "Good.\nVery.")
    b = con.execute("SELECT triaged_at FROM ads WHERE uuid='b'").fetchone()
    assert b["triaged_at"] is None  # no result: retried at the next run
