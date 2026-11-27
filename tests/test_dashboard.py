import pytest
from dotenv import dotenv_values

from jobalerts import dashboard


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
    r = client.post("/api/settings", base_url=BASE, headers=H, json={
        "CANDIDATE_NAME": "Kari", "LANGUAGE": "en", "TELEGRAM_BOT_TOKEN": "",  # empty secret = keep
        "area": {"municipalities": ["OSLO", "ASKER"], "extra": "sandvika, Fornebu", "counties": ["Oslo"]},
    })
    assert r.status_code == 200, r.get_json()
    values = dotenv_values(env_file)
    assert values["CANDIDATE_NAME"] == "Kari"
    assert values["TELEGRAM_BOT_TOKEN"] == "secret:1"
    assert values["TARGET_MUNICIPALITIES"] == "ASKER,FORNEBU,OSLO,SANDVIKA"
    assert values["TARGET_COUNTIES"] == "OSLO"


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
    r = client.post("/api/settings", base_url=BASE, headers=H, json={
        "SOURCE_NAV": "false", "SOURCE_LINKEDIN": "on", "GMAIL_LINKEDIN_LABEL": "linkedin-jobs"})
    assert r.status_code == 200
    values = dotenv_values(env_file)
    assert (values["SOURCE_NAV"], values["SOURCE_LINKEDIN"], values["GMAIL_LINKEDIN_LABEL"]) == ("false", "true", "linkedin-jobs")


def test_default_area_is_stored_empty(client, env_file):
    defaults = sorted(dashboard.DEFAULT_MUNICIPALITIES)
    r = client.post("/api/settings", base_url=BASE, headers=H, json={
        "area": {"municipalities": defaults, "extra": "", "counties": ["Oslo", "Akershus"]}})
    assert r.status_code == 200
    values = dotenv_values(env_file)
    assert values["TARGET_MUNICIPALITIES"] == "" and values["TARGET_COUNTIES"] == ""


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
    r = client.post("/api/tasks", base_url=BASE, headers=H, json={"morning": "7h30"})
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
    missing = [s for s in dict.fromkeys(found) if s not in fr and s not in names]
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
    from jobalerts import config, telegram, texts
    sent = []
    monkeypatch.setattr(config, "LANGUAGE", config.LANGUAGE)  # restored after the test
    monkeypatch.setattr(telegram, "send_text", lambda text: sent.append(text))
    monkeypatch.setattr(dashboard, "reload_settings", lambda: setattr(config, "LANGUAGE", dotenv_values(env_file)["LANGUAGE"]))
    r = client.post("/api/language", base_url=BASE, headers=H, json={"alerts": "fr"}).get_json()
    assert r["telegram_sent"] is True and dotenv_values(env_file)["LANGUAGE"] == "fr"
    assert sent == [texts.TEXTS["fr"]["language_set"]]
    r = client.post("/api/language", base_url=BASE, headers=H, json={"dashboard": "fr"}).get_json()
    assert r["telegram_sent"] is None and dotenv_values(env_file)["DASHBOARD_LANGUAGE"] == "fr" and len(sent) == 1
    assert client.post("/api/language", base_url=BASE, headers=H, json={"alerts": "de"}).status_code == 400


def test_language_saved_even_if_telegram_fails(client, env_file, monkeypatch):
    from jobalerts import telegram

    def boom(text):
        raise RuntimeError("no token")
    monkeypatch.setattr(telegram, "send_text", boom)
    r = client.post("/api/language", base_url=BASE, headers=H, json={"alerts": "en"}).get_json()
    assert r["ok"] and r["telegram_sent"] is False and "no token" in r["telegram_error"]
    assert dotenv_values(env_file)["LANGUAGE"] == "en"


REMOTE = "https://my-pc.tail1234.ts.net:8787"


@pytest.fixture
def remote(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_REMOTE_HOST", "my-pc.tail1234.ts.net")
    monkeypatch.setenv("DASHBOARD_SECRET", "test-secret")
    monkeypatch.delenv("DASHBOARD_PASSWORD_HASH", raising=False)
    monkeypatch.setattr(dashboard.app, "secret_key", None)
    monkeypatch.setattr(dashboard, "_login_failures", [])
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
    remote.post("/api/remote", base_url=BASE, headers=H, json={"password": "another one"})
    assert remote.get("/", base_url=REMOTE).status_code == 302


def test_login_is_throttled(remote):
    remote.post("/api/remote", base_url=BASE, headers=H, json={"password": "correct horse"})
    for _ in range(5):
        remote.post("/login", base_url=REMOTE, data={"password": "nope nope"})
    assert remote.post("/login", base_url=REMOTE, data={"password": "correct horse"}).status_code == 429


@pytest.mark.parametrize("body, error", [
    ({"password": "short"}, "8 characters"),
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
