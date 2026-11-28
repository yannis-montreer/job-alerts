"""Claude login: the yearly token, its status, and the admin's Telegram alerts."""
import json
from datetime import date, timedelta

import pytest

from jobalerts import claude_auth, config, db, profiles, telegram


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(config, "AI_PROVIDER", "claude")
    return tmp_path / ".env"


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(telegram, "send_text", lambda text, bot=None: out.append((bot.id, text)))
    return out


def test_the_token_goes_to_the_claude_command_and_never_an_api_key(env_file, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-api")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in claude_auth.cli_env()
    env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-123\nCLAUDE_TOKEN_CREATED=2026-11-28\n", encoding="utf-8")
    env = claude_auth.cli_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-123" and "ANTHROPIC_API_KEY" not in env
    assert claude_auth.token_created() == date(2026, 11, 28)


def test_status_of_the_token_counts_a_year(env_file, monkeypatch, tmp_path):
    created = date.today() - timedelta(days=360)
    env_file.write_text(f"CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-123\nCLAUDE_TOKEN_CREATED={created}\n", encoding="utf-8")
    exe = tmp_path / "claude.exe"
    exe.write_text("")

    class P:
        stdout = json.dumps({"loggedIn": True, "subscriptionType": "pro"})
    seen = {}
    monkeypatch.setattr(claude_auth.subprocess, "run", lambda cmd, **k: seen.update(env=k["env"]) or P())
    st = claude_auth.status(exe)
    assert st["mode"] == "token" and st["logged_in"] and st["days_left"] in (4, 5)
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-123"


def test_expiry_warnings_go_once_per_step_to_the_admin_bot(env_file, con, sent):
    st = dict(installed=True, logged_in=True, mode="login", expires="2026-12-05T18:30:00+00:00", days_left=6)
    claude_auth.check_expiry(con, st)
    claude_auth.check_expiry(con, st)
    assert len(sent) == 1 and sent[0][0] == "b1" and "6 day(s)" in sent[0][1] and "/#ai" in sent[0][1]
    claude_auth.check_expiry(con, dict(st, days_left=2))
    claude_auth.check_expiry(con, dict(st, days_left=0))
    assert len(sent) == 3
    claude_auth.check_expiry(con, dict(st, days_left=20))
    assert len(sent) == 3


def test_logged_out_and_login_failures_alert_once_a_day(env_file, con, sent):
    claude_auth.check_expiry(con, dict(installed=True, logged_in=False, mode="login"))
    claude_auth.check_expiry(con, dict(installed=True, logged_in=False, mode="login"))
    assert len(sent) == 1 and sent[0][1].startswith("❌")
    claude_auth.report_failure(con, "claude -p failed (code 1): Invalid API key · Please run /login")
    claude_auth.report_failure(con, "claude -p failed (code 1): Invalid API key · Please run /login")
    claude_auth.report_failure(con, "Edge missing")  # not a login problem
    assert len(sent) == 2


def test_nothing_without_claude_or_without_an_admin_bot(env_file, con, sent, monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "gemini")
    claude_auth.check_expiry(con, dict(installed=True, logged_in=False))
    monkeypatch.setattr(config, "AI_PROVIDER", "claude")
    data = profiles.load()
    data["bots"][0].owner = "u1"
    profiles.save(data)
    claude_auth.report_failure(con, "not logged in")
    assert sent == []


def test_the_dashboard_dates_a_saved_token(tmp_path, monkeypatch):
    from jobalerts import dashboard
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(dashboard, "ENV_PATH", env)
    monkeypatch.setattr(dashboard, "PORT", 8765)
    monkeypatch.setattr(dashboard, "TOKEN", "t0k")
    c = dashboard.app.test_client()
    H = {"X-Token": "t0k"}
    r = c.post("/api/settings", base_url="http://127.0.0.1:8765", headers=H, json={"CLAUDE_CODE_OAUTH_TOKEN": "bad token"})
    assert r.status_code == 400
    c.post("/api/settings", base_url="http://127.0.0.1:8765", headers=H, json={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-abcdefghijklmnop"})
    text = env.read_text(encoding="utf-8")
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-abcdefghijklmnop" in text and f"CLAUDE_TOKEN_CREATED={date.today()}" in text
