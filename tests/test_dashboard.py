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


def test_default_area_is_stored_empty(client, env_file):
    defaults = sorted(dashboard.DEFAULT_MUNICIPALITIES)
    r = client.post("/api/settings", base_url=BASE, headers=H, json={
        "area": {"municipalities": defaults, "extra": "", "counties": ["Oslo", "Akershus"]}})
    assert r.status_code == 200
    values = dotenv_values(env_file)
    assert values["TARGET_MUNICIPALITIES"] == "" and values["TARGET_COUNTIES"] == ""


@pytest.mark.parametrize("body, error", [
    ({"LANGUAGE": "de"}, "Language"),
    ({"TELEGRAM_CHAT_ID": "abc"}, "chat id"),
    ({"area": {"municipalities": [], "extra": "", "counties": []}}, "municipality"),
])
def test_save_settings_validation(client, body, error):
    r = client.post("/api/settings", base_url=BASE, headers=H, json=body)
    assert r.status_code == 400 and error in r.get_json()["error"]


def test_task_time_validation(client):
    r = client.post("/api/tasks", base_url=BASE, headers=H, json={"morning": "7h30"})
    assert r.status_code == 400 and "HH:MM" in r.get_json()["error"]
