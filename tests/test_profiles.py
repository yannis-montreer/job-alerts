"""Several profiles: each gets the ads of its own area and alert mailbox, is sorted on its own,
and is sent through its own bot; clicks are recorded for the right profile."""
import json

import pytest

from conftest import add_ad
from jobalerts import config, db, finn, mail, profiles, telegram, triage

ALERT_A = """Nye treff for søket ditt

Business Developer
Acme AS, Oslo
Flere detaljer: https://www.finn.no/111
"""
ALERT_B = """Nye treff for søket ditt

Barista
Kaffe AS, Drammen
Flere detaljer: https://www.finn.no/222

Business Developer
Acme AS, Oslo
Flere detaljer: https://www.finn.no/111
"""


@pytest.fixture
def two(tmp_path, monkeypatch):
    for key, value in (("TELEGRAM_BOT_TOKEN", "111:one"), ("BOT_B2_TOKEN", "222:two")):
        monkeypatch.setenv(key, value)
    (tmp_path / "p1.md").write_text("Alex: B2B sales.", encoding="utf-8")
    (tmp_path / "p2.md").write_text("Anna: barista.", encoding="utf-8")
    profiles.PATH.write_text(json.dumps({
        "profiles": [
            dict(id="p1", name="Alex", candidate="Alex", language="en", municipalities=["OSLO"], counties=["OSLO"],
                 bot="b1", profile_file=str(tmp_path / "p1.md"), preferences_file=str(tmp_path / "p1.json")),
            dict(id="p2", name="Anna", candidate="Kari", language="fr", municipalities=["DRAMMEN", "OSLO"],
                 counties=[], finn_label="finn-anna", bot="b2",
                 profile_file=str(tmp_path / "p2.md"), preferences_file=str(tmp_path / "p2.json")),
        ],
        "bots": [dict(id="b1", name="one", chat_id="42", token_env="TELEGRAM_BOT_TOKEN"),
                 dict(id="b2", name="two", chat_id="77", token_env="BOT_B2_TOKEN")],
    }), encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_FINN", True)
    monkeypatch.setattr(mail, "credentials", lambda: ("a@b", "pw"))
    return profiles.all_profiles()


def test_finn_ads_go_to_the_profiles_of_their_mailbox_and_area(con, two, monkeypatch):
    boxes = []

    def texts(sender, box, since):
        boxes.append(box)
        return [ALERT_A] if box == "INBOX" else [ALERT_B]
    monkeypatch.setattr(mail, "alert_texts", texts)
    finn.fetch_new_ads(con)
    assert sorted(boxes) == ["INBOX", "finn-anna"]
    got = {(r["profile_id"], r["uuid"]) for r in con.execute("SELECT * FROM matches")}
    # 111 is in both alerts and both areas; 222 (Drammen) only in Anna's alert and area
    assert got == {("p1", "finn:111"), ("p2", "finn:111"), ("p2", "finn:222")}
    assert db.get_meta(con, "finn_last_success:finn-anna")


def test_each_profile_is_sorted_with_its_own_profile(con, two, monkeypatch):
    add_ad(con, "a", profile="p1")
    db.add_match(con, "p2", "a")
    prompts = []
    monkeypatch.setattr(triage, "call_ai", lambda prompt: prompts.append(prompt) or [
        dict(id=1, relevant=True, score=4, summary="ok", norwegian_required=False, reason="r")])
    for p in two:
        with profiles.use(p):
            assert triage.triage(con) == 1
    assert "Alex: B2B sales." in prompts[0] and "Anna: barista." in prompts[1]
    assert "You help Alex sort" in prompts[0] and "You help Kari sort" in prompts[1]
    assert "in French" in prompts[1]


def test_each_profile_sends_with_its_bot_and_clicks_are_recorded_for_it(con, two, monkeypatch):
    add_ad(con, "a", profile="p1", triaged_at="x", relevant=1, score=5, summary="s1")
    db.add_match(con, "p2", "a")
    con.execute("UPDATE matches SET triaged_at='x', relevant=1, score=5, summary='s2' WHERE profile_id='p2'")
    con.commit()
    calls = []

    def fake_api(method, _timeout=30, bot=None, **params):
        calls.append((bot.id if bot else None, method, params))
        return {"message_id": 500 + len(calls)}
    monkeypatch.setattr(telegram, "api", fake_api)
    for p in two:
        with profiles.use(p):
            telegram.send_offers(con)
    sends = [(b, p["chat_id"], p["reply_markup"]["inline_keyboard"][0][0]) for b, m, p in calls if m == "sendMessage"]
    assert sends[0][:2] == ("b1", 42) and sends[0][2]["callback_data"] == "k:p1:a" and sends[0][2]["text"] == "Keep"
    assert sends[1][:2] == ("b2", 77) and sends[1][2]["callback_data"] == "k:p2:a" and sends[1][2]["text"] == "Garder"
    assert {(r["bot_id"], r["profile_id"]) for r in con.execute("SELECT * FROM messages")} == {("b1", "p1"), ("b2", "p2")}

    # Anna taps Keep on bot two: the decision is Anna's only
    owner = {"id": 77}
    batches = [[{"update_id": 9, "callback_query": {"id": "c", "from": owner, "data": "k:p2:a",
                                                     "message": {"message_id": 502, "chat": owner}}}], []]
    monkeypatch.setattr(telegram, "api", lambda method, _timeout=30, bot=None, **p:
                        batches.pop(0) if method == "getUpdates" else {})
    telegram.collect_updates(con, bot=profiles.get_bot("b2"))
    assert [tuple(r) for r in con.execute("SELECT profile_id, bot_id, decision FROM decisions")] == [("p2", "b2", "keep")]
    assert db.get_meta(con, "tg_offset:b2") == "10" and db.get_meta(con, "tg_offset:b1") is None


def test_old_buttons_count_for_the_bots_first_profile(two):
    bot = profiles.get_bot("b1")
    assert telegram.parse_callback("k:abc-123", bot) == ("k", "p1", "abc-123")
    assert telegram.parse_callback("r:finn:555", bot) == ("r", "p1", "finn:555")
    assert telegram.parse_callback("k:p2:finn:555", bot) == ("k", "p2", "finn:555")


def test_a_failing_or_unfinished_profile_does_not_stop_the_others(con, two, monkeypatch):
    from jobalerts import run
    done = []

    def run_profile(c, p):
        if p.id == "p2":
            raise RuntimeError("AI down")
        done.append(p.id)
    monkeypatch.setattr(run, "run_profile", run_profile)
    monkeypatch.setattr(run, "collect_clicks", lambda c: None)
    monkeypatch.setattr(config, "SOURCE_NAV", False)
    monkeypatch.setattr(config, "SOURCE_FINN", False)
    monkeypatch.setattr(run, "check_claude_login", lambda: None)
    monkeypatch.setattr(run.db, "connect", lambda: con)
    monkeypatch.setattr(telegram, "send_text", lambda *a, **k: None)
    assert run.run(collect_only=False) == 1 and done == ["p1"]


def test_migration_keeps_the_old_history(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE ads (uuid TEXT PRIMARY KEY, first_seen TEXT NOT NULL, date_modified TEXT, title TEXT, employer TEXT,
            municipal TEXT, url TEXT, application_url TEXT, description TEXT, source TEXT NOT NULL DEFAULT 'nav',
            triaged_at TEXT, relevant INTEGER, score INTEGER, summary_fr TEXT, norwegian_required INTEGER, sent_at TEXT);
        CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL, decision TEXT NOT NULL,
            reason TEXT, decided_at TEXT NOT NULL, telegram_update_id INTEGER UNIQUE);
        CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL, text TEXT NOT NULL,
            telegram_update_id INTEGER UNIQUE);
        CREATE TABLE sent_messages (message_id INTEGER PRIMARY KEY, uuid TEXT NOT NULL, sent_at TEXT NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO ads VALUES ('a','t',NULL,'Job','Acme','OSLO','u',NULL,'d','nav','x',1,4,'résumé',0,'s');
        INSERT INTO decisions(uuid, decision, decided_at, telegram_update_id) VALUES ('a','reject','x',5);
        INSERT INTO notes(uuid, text, telegram_update_id) VALUES ('a','too junior',6);
        INSERT INTO sent_messages VALUES (7,'a','s');
        INSERT INTO meta VALUES ('tg_offset','7');
    """)
    old.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    con = db.connect()
    db.connect().close()  # twice: nothing changes
    assert tuple(con.execute("SELECT profile_id, score, summary, sent_at FROM matches").fetchone()) == ("p1", 4, "résumé", "s")
    assert tuple(con.execute("SELECT profile_id, bot_id, decision FROM decisions").fetchone()) == ("p1", "b1", "reject")
    assert tuple(con.execute("SELECT profile_id, text FROM notes").fetchone()) == ("p1", "too junior")
    assert tuple(con.execute("SELECT bot_id, message_id, profile_id FROM messages").fetchone()) == ("b1", 7, "p1")
    assert db.get_meta(con, "tg_offset:b1") == "7"
    assert con.execute("SELECT count(*) FROM matches").fetchone()[0] == 1


def test_profiles_being_set_up_are_skipped_quietly(con, two, monkeypatch):
    from jobalerts import run
    data = profiles.load()
    data["profiles"][1].bot = ""
    profiles.save(data)
    assert profiles.issue(profiles.get("p2")) == "no Telegram bot chosen" and profiles.issue(profiles.get("p1")) == ""
    done = []
    monkeypatch.setattr(run, "run_profile", lambda c, p: done.append(p.id))
    monkeypatch.setattr(run, "collect_clicks", lambda c: None)
    monkeypatch.setattr(config, "SOURCE_NAV", False)
    monkeypatch.setattr(config, "SOURCE_FINN", False)
    monkeypatch.setattr(run, "check_claude_login", lambda: None)
    monkeypatch.setattr(run.db, "connect", lambda: con)
    assert run.run(collect_only=False) == 0 and done == ["p1"]


def test_a_profile_run_sorts_and_sends_only_that_profile(con, two, monkeypatch):
    from jobalerts import run
    done = []
    monkeypatch.setattr(run, "run_profile", lambda c, p: done.append(p.id))
    monkeypatch.setattr(run, "collect_clicks", lambda c: None)
    monkeypatch.setattr(config, "SOURCE_NAV", False)
    monkeypatch.setattr(config, "SOURCE_FINN", False)
    monkeypatch.setattr(run, "check_claude_login", lambda: None)
    monkeypatch.setattr(run.db, "connect", lambda: con)
    assert run.run(collect_only=False, profile_id="p2") == 0 and done == ["p2"]
    with pytest.raises(RuntimeError):
        run.run(collect_only=False, profile_id="p7")


def test_full_runs_wait_for_each_other(tmp_path, monkeypatch):
    import threading
    import time as _time
    from jobalerts import run
    monkeypatch.setattr(config, "ROOT", tmp_path)
    order = []

    def worker(name):
        with run.RunLock(wait_s=30, poll_s=0.05):
            order.append(f"{name}-in")
            _time.sleep(0.3)
            order.append(f"{name}-out")
    a = threading.Thread(target=worker, args=("a",))
    a.start()
    _time.sleep(0.1)
    worker("b")
    a.join()
    assert order == ["a-in", "a-out", "b-in", "b-out"]
