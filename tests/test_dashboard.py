import json

import pytest
from dotenv import dotenv_values

from jobalerts import dashboard, profiles, users


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("# my comment\nCANDIDATE_NAME=Alex\nTELEGRAM_BOT_TOKEN=secret:1\n", encoding="utf-8")
    monkeypatch.setattr(dashboard, "ENV_PATH", path)
    monkeypatch.setattr(dashboard, "reload_settings", lambda: None)
    return path


@pytest.fixture
def client(env_file, monkeypatch):
    monkeypatch.setattr(dashboard, "PORT", 8765)
    monkeypatch.setattr(dashboard, "TOKEN", "t0k")
    return dashboard.app.test_client()


H = {"X-Token": "t0k"}
BASE = "http://127.0.0.1:8765"


def test_write_env_keeps_comments_and_quotes(env_file):
    dashboard.write_env({"CANDIDATE_NAME": "Anne Marie", "GMAIL_APP_PASSWORD": 'ab cd "ef" #1\\', "LANGUAGE": "fr"})
    text = env_file.read_text(encoding="utf-8")
    assert text.startswith("# my comment\n")
    values = dotenv_values(env_file)
    assert values["CANDIDATE_NAME"] == "Anne Marie"
    assert values["GMAIL_APP_PASSWORD"] == 'ab cd "ef" #1\\'
    assert values["LANGUAGE"] == "fr"
    assert values["TELEGRAM_BOT_TOKEN"] == "secret:1"


def test_write_env_rejects_newlines(env_file):
    with pytest.raises(dashboard.UserError):
        dashboard.write_env({"CANDIDATE_NAME": "a\nb"})


def test_requires_local_host_and_token(client):
    assert client.get("/api/settings", base_url="http://evil.example:8765", headers=H).status_code == 403
    assert client.get("/api/settings", base_url=BASE).status_code == 403
    assert client.get("/api/settings", base_url=BASE, headers={"X-Token": "wrong"}).status_code == 403
    assert client.get("/api/settings", base_url=BASE, headers=H).status_code == 200


def test_page_embeds_token(client):
    r = client.get("/", base_url=BASE)
    assert r.status_code == 200 and b'const TOKEN = "t0k"' in r.data


def test_settings_never_return_secrets(client):
    data = client.get("/api/settings", base_url=BASE, headers=H).get_json()
    assert data["secrets_set"]["TELEGRAM_BOT_TOKEN"] is True
    assert "secret:1" not in str(data)


def test_save_settings_area_and_secrets(client, env_file):
    from jobalerts import profiles
    r = client.post("/api/settings", base_url=BASE, headers=H, json={
        "CANDIDATE_NAME": "Kari", "LANGUAGE": "en", "TELEGRAM_BOT_TOKEN": "",  # empty secret = keep
        "area": {"municipalities": ["OSLO", "ASKER"], "extra": "sandvika, Fornebu", "counties": ["Oslo"]},
    })
    assert r.status_code == 200, r.get_json()
    p = profiles.all_profiles()[0]  # profile settings: profiles.json
    assert p.candidate == "Kari"
    assert p.municipalities == ["ASKER", "FORNEBU", "OSLO", "SANDVIKA"] and p.counties == ["OSLO"]
    assert dotenv_values(env_file)["TELEGRAM_BOT_TOKEN"] == "secret:1"


def test_save_gemini_settings(client, env_file):
    r = client.post("/api/settings", base_url=BASE, headers=H, json={
        "AI_PROVIDER": "gemini", "GEMINI_API_KEY": "AIza-test", "GEMINI_MODEL": "gemini-3.8-flash"})
    assert r.status_code == 200
    values = dotenv_values(env_file)
    assert (values["AI_PROVIDER"], values["GEMINI_API_KEY"], values["GEMINI_MODEL"]) == ("gemini", "AIza-test", "gemini-3.8-flash")
    data = client.get("/api/settings", base_url=BASE, headers=H).get_json()
    assert data["secrets_set"]["GEMINI_API_KEY"] is True and "AIza-test" not in str(data)


def test_preferences_api(client):
    r = client.post("/api/preferences", base_url=BASE, headers=H, json={"preferences": {
        "target_roles": [{"name": "Barista", "level": "also"}], "exclude": ["Call center"]}})
    assert r.status_code == 200 and "Barista" in r.get_json()["prompt"]
    data = client.get("/api/preferences", base_url=BASE, headers=H).get_json()
    assert data["preferences"]["exclude"] == ["Call center"]


def test_feedback_lists_clicks_and_replies(client, con):
    from conftest import add_ad
    add_ad(con, "old", title="Telefonselger")
    add_ad(con, "clicked", title="SDR")
    add_ad(con, "silent", title="Nothing")
    con.execute("INSERT INTO notes(uuid, text) VALUES('old', 'Téléprospection précaire')")
    con.execute("INSERT INTO decisions(uuid, decision, decided_at) VALUES('clicked', 'keep', 'x')")
    con.commit()
    rows = client.get("/api/feedback", base_url=BASE, headers=H).get_json()["rows"]
    assert {r["title"]: (r["decision"], r["notes"]) for r in rows} == {
        "Telefonselger": (None, "Téléprospection précaire"), "SDR": ("keep", None)}


def test_source_switches_are_saved_as_booleans(client, env_file):
    from jobalerts import profiles
    r = client.post("/api/settings", base_url=BASE, headers=H, json={
        "SOURCE_NAV": "false", "SOURCE_LINKEDIN": "on", "GMAIL_LINKEDIN_LABEL": "linkedin-jobs"})
    assert r.status_code == 200
    values = dotenv_values(env_file)
    assert (values["SOURCE_NAV"], values["SOURCE_LINKEDIN"]) == ("false", "true")
    assert profiles.all_profiles()[0].linkedin_label == "linkedin-jobs"


def test_default_area_is_stored_empty(client, env_file):
    from jobalerts import profiles
    defaults = sorted(dashboard.DEFAULT_MUNICIPALITIES)
    r = client.post("/api/settings", base_url=BASE, headers=H, json={
        "area": {"municipalities": defaults, "extra": "", "counties": ["Oslo", "Akershus"]}})
    assert r.status_code == 200
    p = profiles.all_profiles()[0]
    assert p.municipalities == [] and p.counties == []


@pytest.mark.parametrize("body, error", [
    ({"LANGUAGE": "de"}, "Language"),
    ({"AI_PROVIDER": "chatgpt"}, "claude or gemini"),
    ({"GEMINI_MODEL": "bad model!"}, "model id"),
    ({"FINN_FETCH_PAGES": "true"}, "contact email"),
    ({"NORWEGIAN_REQUIRED": "maybe"}, "flag or reject"),
    ({"MESSAGE_MIN_SCORE": "9"}, "1 to 5"),
    ({"MESSAGE_MIN_SCORE": "3", "DIGEST_MIN_SCORE": "4"}, "must not be above"),
    ({"FINN_CONTACT_EMAIL": "not-an-email"}, "email looks wrong"),
    ({"TELEGRAM_CHAT_ID": "abc"}, "chat id"),
    ({"area": {"municipalities": [], "extra": "", "counties": []}}, "municipality"),
])
def test_save_settings_validation(client, body, error):
    r = client.post("/api/settings", base_url=BASE, headers=H, json=body)
    assert r.status_code == 400 and error in r.get_json()["error"]


def test_task_time_validation(client):
    r = client.post("/api/schedule", base_url=BASE, headers=H, json={"time": "7h30"})
    assert r.status_code == 400 and "HH:MM" in r.get_json()["error"]


def _french():
    import json
    return json.loads(dashboard.PAGE_FR.read_text(encoding="utf-8"))


def test_every_dashboard_text_has_a_french_translation():
    import re
    from html.parser import HTMLParser
    src = dashboard.PAGE.read_text(encoding="utf-8")
    fr = _french()
    body = src[src.index("<body>"):src.index("<script>")]
    found = []

    class P(HTMLParser):
        skip = 0

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if "notr" in (a.get("class") or "").split() or tag in ("code", "pre"):
                self.skip += 1
            for k in ("placeholder", "title"):
                if a.get(k):
                    found.append(a[k])

        def handle_endtag(self, tag):
            pass

        def handle_data(self, d):
            if d.strip():
                found.append(" ".join(d.split()))

    P().feed(body)
    names = {"Claude", "Gemini", "Gmail", "LinkedIn", "Finn.no", "finn.no", "NAV / Arbeidsplassen", "Telegram",
             "@BotFather", "/newbot", "bot", "Google AI Studio", "gemini-flash-latest", "English", "Français",
             "EN", "🌐", ".", "…", "2", "3", "4", "5"}
    missing = [s for s in dict.fromkeys(found) if s not in fr and s not in names and re.search(r"[^\W\d_]", s)]  # icons need none
    script = src[src.index("<script>"):]
    missing += [s for s in re.findall(r'\btr\("((?:[^"\\]|\\.)*)"', script) + re.findall(r"\btr\('((?:[^'\\]|\\.)*)'", script)
                if s.replace('\\"', '"') not in fr]
    missing += [s for s in re.findall(r'\["(?:ads|sorted|relevant|sent|kept|rejected)", "([a-z ]+)"\]', script) if s not in fr]  # stat labels
    assert not missing, missing


def test_french_placeholders_match_english():
    import re
    for en, fr in _french().items():
        assert set(re.findall(r"\{(\w+)\}", en)) == set(re.findall(r"\{(\w+)\}", fr)), en


def test_page_inlines_french_and_ui_language(client, env_file):
    env_file.write_text("DASHBOARD_LANGUAGE=fr\n", encoding="utf-8")
    r = client.get("/", base_url=BASE).get_data(as_text=True)
    assert 'let UI = "fr"' in r and '"Overview": "Vue d\'ensemble"' in r and "{{" not in r


def test_language_endpoint_confirms_in_telegram(client, env_file, monkeypatch):
    from jobalerts import profiles, telegram, texts
    sent = []
    monkeypatch.setattr(telegram, "send_text", lambda text: sent.append(text))
    r = client.post("/api/language", base_url=BASE, headers=H, json={"alerts": "fr"}).get_json()
    assert r["telegram_sent"] is True and profiles.all_profiles()[0].language == "fr"
    assert sent == [texts.TEXTS["fr"]["language_set"]]
    r = client.post("/api/language", base_url=BASE, headers=H, json={"dashboard": "fr"}).get_json()
    assert r["telegram_sent"] is None and dotenv_values(env_file)["DASHBOARD_LANGUAGE"] == "fr" and len(sent) == 1
    assert client.post("/api/language", base_url=BASE, headers=H, json={"alerts": "de"}).status_code == 400


def test_language_saved_even_if_telegram_fails(client, env_file, monkeypatch):
    from jobalerts import telegram

    def boom(text):
        raise RuntimeError("no token")
    monkeypatch.setattr(telegram, "send_text", boom)
    r = client.post("/api/language", base_url=BASE, headers=H, json={"alerts": "fr"}).get_json()
    assert r["ok"] and r["telegram_sent"] is False and "no token" in r["telegram_error"]
    from jobalerts import profiles
    assert profiles.all_profiles()[0].language == "fr"


REMOTE = "https://my-pc.tail1234.ts.net:8787"


@pytest.fixture
def remote(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_REMOTE_HOST", "my-pc.tail1234.ts.net")
    monkeypatch.setenv("DASHBOARD_SECRET", "test-secret")
    monkeypatch.delenv("DASHBOARD_PASSWORD_HASH", raising=False)
    monkeypatch.setattr(dashboard.app, "secret_key", None)
    dashboard.ensure_secret_key()  # done by main() at start
    monkeypatch.setattr(dashboard, "_login_failures", {})
    monkeypatch.setattr(dashboard.time, "sleep", lambda s: None)
    # write_env is replaced by a version that also updates os.environ (reload_settings is a no-op here)
    real_write = dashboard.write_env

    def write(updates):
        real_write(updates)
        for k, v in updates.items():
            monkeypatch.setenv(k, v)
    monkeypatch.setattr(dashboard, "write_env", write)
    return client


def test_unknown_hosts_are_still_refused(remote):
    assert remote.get("/", base_url="https://evil.ts.net:8787").status_code == 403


def test_remote_needs_login(remote):
    r = remote.get("/", base_url=REMOTE)
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert remote.get("/api/settings", base_url=REMOTE, headers=H).status_code == 401
    assert remote.get("/manifest.webmanifest", base_url=REMOTE).status_code == 200
    assert b"No password is set yet" in remote.get("/login", base_url=REMOTE).data


def test_remote_login_flow(remote):
    r = remote.post("/api/remote", base_url=BASE, headers=H, json={"host": "https://My-PC.tail1234.ts.net:8787/", "password": "correct horse"})
    assert r.status_code == 200 and r.get_json()["password_set"] and r.get_json()["host"] == "my-pc.tail1234.ts.net"
    assert remote.post("/login", base_url=REMOTE, data={"password": "wrong pass"}).status_code == 401
    r = remote.post("/login", base_url=REMOTE, data={"password": "correct horse"})
    assert r.status_code == 302
    page = remote.get("/", base_url=REMOTE)
    assert page.status_code == 200 and b"const REMOTE = true" in page.data
    assert remote.get("/api/settings", base_url=REMOTE, headers=H).status_code == 200
    # a new password logs the phone out
    remote.post("/api/remote", base_url=BASE, headers=H, json={"password": "another one!!"})
    assert remote.get("/", base_url=REMOTE).status_code == 302


def test_login_is_throttled(remote):
    remote.post("/api/remote", base_url=BASE, headers=H, json={"password": "correct horse"})
    for _ in range(5):
        remote.post("/login", base_url=REMOTE, data={"password": "nope nope"})
    assert remote.post("/login", base_url=REMOTE, data={"password": "correct horse"}).status_code == 429


@pytest.mark.parametrize("body, error", [
    ({"password": "short"}, "12 characters"),
    ({"host": "not a host"}, "looks wrong"),
    ({"host": "localhost"}, "Tailscale address"),
])
def test_remote_settings_validation(remote, body, error):
    r = remote.post("/api/remote", base_url=BASE, headers=H, json=body)
    assert r.status_code == 400 and error in r.get_json()["error"]


def test_secrets_of_remote_access_are_never_returned(remote):
    remote.post("/api/remote", base_url=BASE, headers=H, json={"password": "correct horse"})
    data = str(remote.get("/api/settings", base_url=BASE, headers=H).get_json())
    assert "DASHBOARD_PASSWORD_HASH" not in data and "test-secret" not in data


def test_profiles_and_bots(client, env_file, monkeypatch, tmp_path):
    from jobalerts import profiles
    # the first save turns the .env setup into profile p1 with bot b1
    client.post("/api/settings", base_url=BASE, headers=H, json={"PROFILE_NAME": "Alex"})
    r = client.post("/api/profiles", base_url=BASE, headers=H, json={"name": "Anna"}).get_json()
    assert r["id"] == "p2"
    assert client.post("/api/profiles", base_url=BASE, headers=H, json={"name": "anna"}).status_code == 400
    h2 = H | {"X-Profile": "p2"}
    data = client.get("/api/settings", base_url=BASE, headers=h2).get_json()
    assert data["profile"] == "p2" and data["values"]["BOT"] == "" and data["secrets_set"]["TELEGRAM_BOT_TOKEN"] is False
    assert {p["id"]: p["issue"] for p in data["profiles"]}["p2"] == "no Telegram bot chosen"
    # a second bot for Anna: token in .env under its own key, chat id in profiles.json
    assert client.post("/api/bots", base_url=BASE, headers=h2, json={"name": "Anna bot"}).get_json()["id"] == "b2"
    r = client.post("/api/settings", base_url=BASE, headers=h2, json={
        "TELEGRAM_BOT_TOKEN": "222:abc", "TELEGRAM_CHAT_ID": "77", "CANDIDATE_NAME": "Kari",
        "GMAIL_FINN_LABEL": "finn-anna"})
    assert r.status_code == 200, r.get_json()
    assert dotenv_values(env_file)["BOT_B2_TOKEN"] == "222:abc"
    assert dotenv_values(env_file)["TELEGRAM_BOT_TOKEN"] == "secret:1"  # Alex's bot untouched
    p1, p2 = profiles.all_profiles()
    assert (p1.name, p1.bot, p2.bot, p2.candidate, p2.finn_label) == ("Alex", "b1", "b2", "Kari", "finn-anna")
    assert profiles.get_bot("b2").chat_id == "77"
    # each profile has its own profile text
    client.post("/api/profile", base_url=BASE, headers=h2, json={"text": "Kari: barista, 5 years, Oslo. " * 3})
    assert p2.profile_path.read_text(encoding="utf-8").startswith("Kari: barista")
    assert "Kari" not in client.get("/api/profile", base_url=BASE, headers=H).get_json()["text"]
    # unknown bot refused; the last profile cannot be deleted
    assert client.post("/api/settings", base_url=BASE, headers=h2, json={"BOT": "b9"}).status_code == 400
    assert client.post("/api/profiles/delete", base_url=BASE, headers=H, json={"id": "p2"}).status_code == 200
    assert client.post("/api/profiles/delete", base_url=BASE, headers=H, json={"id": "p1"}).status_code == 400


def test_one_task_per_profile_at_its_time(client, env_file, monkeypatch):
    from jobalerts import profiles
    client.post("/api/settings", base_url=BASE, headers=H, json={"PROFILE_NAME": "Alex"})
    client.post("/api/profiles", base_url=BASE, headers=H, json={"name": "Anna"})
    tasks = [  # before: one morning task for every profile, an old catch-up, a task of a deleted profile
        dict(name="Old-Morning", kind="full run", profile=None, start="07:15", next="n", last="", result=None, every=""),
        dict(name="Old-CatchUp", kind="catch-up", profile=None, start="07:40", next="n", last="", result=0, every="PT10M"),
        dict(name="JobAlerts-p9", kind="profile", profile="p9", start="06:00", next="n", last="", result=0, every=""),
    ]
    calls = []
    monkeypatch.setattr(dashboard, "list_tasks", lambda: tasks)
    monkeypatch.setattr(dashboard, "schedule_task", lambda name, arguments="", at="", remove=False, **kw:
                        calls.append((name, arguments, at, remove)) or "")
    monkeypatch.setattr(dashboard, "PYTHONW", env_file)  # any existing file
    r = client.post("/api/schedule", base_url=BASE, headers=H | {"X-Profile": "p2"}, json={"time": "09:05"})
    assert r.status_code == 200, r.get_json()
    assert ("JobAlerts-p1", "-m jobalerts.run --profile p1", "07:15", False) in calls  # keeps the old time
    assert ("JobAlerts-p2", "-m jobalerts.run --profile p2", "09:05", False) in calls
    assert ("Old-Morning", "", "", True) in calls and ("JobAlerts-p9", "", "", True) in calls
    assert not any(c[0] == "Old-CatchUp" for c in calls)
    assert [p.send_time for p in profiles.all_profiles()] == ["07:15", "09:05"]
    calls.clear()
    r = client.post("/api/schedule/catchup", base_url=BASE, headers=H, json={"start": "08:00", "hours": 2})
    assert r.status_code == 200
    assert calls == [("Old-CatchUp", "", "", True), ("JobAlerts-CatchUp", "-m jobalerts.run --collect-only", "08:00", False)]


def test_task_kinds_from_arguments(monkeypatch):
    monkeypatch.setattr(dashboard, "powershell", lambda cmd, timeout=60: json.dumps([
        {"name": "a", "args": "-m jobalerts.run --profile p2"}, {"name": "b", "args": "-m jobalerts.run --collect-only"},
        {"name": "c", "args": "-m jobalerts.run"}]))
    from conftest import REAL_LIST_TASKS
    assert [(t["kind"], t["profile"]) for t in REAL_LIST_TASKS()] == [("profile", "p2"), ("catch-up", None), ("full run", None)]


def test_history_shows_the_one_line_summary(client, con):
    from conftest import add_ad
    add_ad(con, "h1", score=4, summary="B2B sales role close to the profile.", sent_at="2026-11-28T06:35:00")
    add_ad(con, "h2", score=5, summary="Not sent yet.")
    rows = client.get("/api/history", base_url=BASE, headers=H).get_json()["rows"]
    assert [(r["uuid"], r["summary"]) for r in rows] == [("h1", "B2B sales role close to the profile.")]


def test_remote_logins_and_lockouts_are_reported(remote, monkeypatch):
    ALERTS = []
    monkeypatch.setattr(dashboard, "alert_owner", ALERTS.append)
    dashboard.ensure_secret_key()  # done by main() at start
    remote.post("/api/remote", base_url=BASE, headers=H, json={"password": "correct horse"})
    r = remote.post("/login", base_url=REMOTE, data={"password": "correct horse"},
                    headers={"X-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 302, r.data[:300]
    assert len(ALERTS) == 1 and "admin logged in from 203.0.113.7" in ALERTS[0]
    for _ in range(5):
        remote.post("/login", base_url=REMOTE, data={"password": "nope nope"})
    assert len(ALERTS) == 2 and "5 wrong passwords" in ALERTS[1]
    r = remote.get("/login", base_url=REMOTE)
    assert r.headers["Strict-Transport-Security"].startswith("max-age=")


@pytest.fixture
def two_alerts(remote):
    """Two profiles: p1 with the admin's bot, p2 with no bot yet; a user "kari" owning p2."""
    data = profiles.load()
    data["profiles"].append(profiles.Profile(id="p2", name="Kari", candidate="Kari", bot=""))
    profiles.save(data)
    r = remote.post("/api/users", base_url=BASE, headers=H,
                    json={"username": "Kari", "password": "kari password 1", "profiles": ["p2"]})
    assert r.status_code == 200 and r.get_json()["user"]["username"] == "kari"
    assert remote.post("/login", base_url=REMOTE, data={"username": "kari", "password": "kari password 1"}).status_code == 302
    return remote


def test_a_user_sees_only_their_alerts(two_alerts):
    c = two_alerts
    s = c.get("/api/settings", base_url=REMOTE, headers=H).get_json()
    assert s["me"] == {"role": "user", "username": "kari"} and [p["id"] for p in s["profiles"]] == ["p2"]
    assert "GMAIL_ADDRESS" not in s["values"] and set(s["secrets_set"]) == {"TELEGRAM_BOT_TOKEN"}
    assert c.get("/api/settings", base_url=REMOTE, headers={**H, "X-Profile": "p1"}).status_code == 403
    for path in ("/api/remote", "/api/users", "/api/gmail/test", "/api/schedule/catchup", "/api/profiles"):
        assert c.post(path, base_url=REMOTE, headers=H, json={}).status_code in (403, 405), path
    st = c.get("/api/status", base_url=REMOTE, headers=H).get_json()
    assert "claude" not in st and "remote" not in st and st["last_runs"] == []
    assert c.get("/api/run", base_url=REMOTE, headers=H).get_json()["log"] == []
    assert c.post("/api/run", base_url=REMOTE, headers=H, json={"mode": "collect"}).status_code == 400
    page = c.get("/", base_url=REMOTE).data
    assert b'const ME = {"role": "user", "username": "kari"}' in page


def test_a_user_changes_their_profile_but_not_the_machine(two_alerts):
    c = two_alerts
    ok = c.post("/api/settings", base_url=REMOTE, headers=H, json={"CANDIDATE_NAME": "Kari N", "PROFILE_NAME": "Kari", "MESSAGE_MIN_SCORE": "4", "DIGEST_MIN_SCORE": "3"})
    assert ok.status_code == 200 and profiles.get("p2").candidate == "Kari N"
    for body in ({"PROFILE_NAME": "Renamed"}, {"GMAIL_FINN_LABEL": "x"}, {"FINN_SUBJECT": "x"}, {"AI_PROVIDER": "gemini"},
                 {"BOT": "b1"}, {"GEMINI_API_KEY": "k"}, {"clear": ["GEMINI_API_KEY"]}):
        r = c.post("/api/settings", base_url=REMOTE, headers=H, json=body)
        assert r.status_code == 400 and "admin" in r.get_json()["error"], body
    assert profiles.get("p2").bot == "" and profiles.get("p2").name == "Kari"


def test_a_user_adds_a_bot_once_then_it_is_locked(two_alerts):
    c = two_alerts
    assert c.post("/api/settings", base_url=REMOTE, headers=H, json={"TELEGRAM_BOT_TOKEN": "1:x"}).status_code == 400
    assert c.post("/api/bots", base_url=REMOTE, headers=H, json={"name": "Kari's bot"}).status_code == 200
    bot = profiles.get_bot(profiles.get("p2").bot)
    assert bot and bot.id != "b1"
    assert c.post("/api/settings", base_url=REMOTE, headers=H, json={"TELEGRAM_BOT_TOKEN": "222:kari"}).status_code == 200
    assert c.post("/api/settings", base_url=REMOTE, headers=H, json={"TELEGRAM_CHAT_ID": "77"}).status_code == 200
    for body in ({"TELEGRAM_BOT_TOKEN": "333:other"}, {"TELEGRAM_CHAT_ID": "78"}, {"BOT_NAME": "new name"}):
        r = c.post("/api/settings", base_url=REMOTE, headers=H, json=body)
        assert r.status_code == 400 and "only the admin" in r.get_json()["error"], body
    assert c.post("/api/bots", base_url=REMOTE, headers=H, json={"name": "again"}).status_code == 400
    assert profiles.get_bot(profiles.get("p2").bot).chat_id == "77"


def test_user_passwords_and_alerts_are_the_admins(two_alerts):
    c = two_alerts
    assert c.post("/api/account", base_url=REMOTE, headers=H, json={"current": "wrong", "new": "x" * 12}).status_code == 400
    assert c.post("/api/account", base_url=REMOTE, headers=H, json={"current": "kari password 1", "new": "short"}).status_code == 400
    assert c.post("/api/account", base_url=REMOTE, headers=H, json={"current": "kari password 1", "new": "kari password 2"}).status_code == 200
    assert c.get("/api/settings", base_url=REMOTE, headers=H).status_code == 200  # still logged in here
    uid = users.by_username("kari").id
    c.post("/api/users/update", base_url=BASE, headers=H, json={"id": uid, "profiles": []})
    s = c.get("/api/settings", base_url=REMOTE, headers=H).get_json()
    assert s["profiles"] == [] and c.get("/api/profile", base_url=REMOTE, headers=H).status_code == 403
    c.post("/api/users/update", base_url=BASE, headers=H, json={"id": uid, "password": "reset by admin"})
    assert c.get("/api/settings", base_url=REMOTE, headers=H).status_code == 401  # logged out
    assert c.post("/login", base_url=REMOTE, data={"username": "kari", "password": "kari password 2"}).status_code == 401
    assert c.post("/api/users", base_url=BASE, headers=H, json={"username": "admin", "password": "x" * 12}).status_code == 400


def test_the_admin_logs_in_as_admin_and_old_sessions_stay_admin(remote):
    remote.post("/api/remote", base_url=BASE, headers=H, json={"password": "correct horse"})
    assert remote.post("/login", base_url=REMOTE, data={"username": "Admin", "password": "correct horse"}).status_code == 302
    s = remote.get("/api/settings", base_url=REMOTE, headers=H).get_json()
    assert s["me"]["role"] == "admin" and "GMAIL_ADDRESS" in s["values"]
    with remote.session_transaction(base_url=REMOTE) as sess:  # a session from before users existed
        sess.pop("uid")
    assert remote.get("/api/users", base_url=REMOTE, headers=H).status_code == 200
