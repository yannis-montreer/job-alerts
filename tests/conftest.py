"""Test setup: fixed settings, never the real .env, database or network."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Set every key BEFORE importing jobalerts: load_dotenv never overrides existing variables,
# so a local .env cannot leak into the tests.
TEST_ENV = {
    "CANDIDATE_NAME": "Alex",
    "LANGUAGE": "en",
    "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
    "TELEGRAM_CHAT_ID": "42",
    "TARGET_MUNICIPALITIES": "",
    "TARGET_COUNTIES": "",
    "NAV_TOKEN": "",
    "GMAIL_ADDRESS": "",
    "GMAIL_APP_PASSWORD": "",
    "GMAIL_FINN_LABEL": "",
    "AI_PROVIDER": "claude",
    "GEMINI_API_KEY": "",
    "GEMINI_MODEL": "",
    "FINN_FETCH_PAGES": "false",
    "FINN_CONTACT_EMAIL": "",
    "MESSAGE_MIN_SCORE": "4",
    "DIGEST_MIN_SCORE": "3",
    "NORWEGIAN_REQUIRED": "flag",
    "SOURCE_NAV": "",
    "SOURCE_FINN": "",
    "SOURCE_LINKEDIN": "",
    "GMAIL_LINKEDIN_LABEL": "",
}
os.environ.update(TEST_ENV)

from jobalerts import config, db, preferences, profiles  # noqa: E402

# Tokens of extra bots (BOT_<ID>_TOKEN) are not in TEST_ENV: drop any read from a local .env.
for _k in [k for k in os.environ if k.startswith("BOT_") and k.endswith("_TOKEN")]:
    del os.environ[_k]


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    """Never read or write the real preferences.json."""
    monkeypatch.setattr(preferences, "PATH", tmp_path / "preferences.json")
    monkeypatch.setattr(profiles, "PATH", tmp_path / "profiles.json")  # no profiles.json: one profile from .env
    monkeypatch.setattr(profiles, "PROFILES_DIR", tmp_path / "profiles")  # never the real profile files
    from jobalerts import cvdocs, users
    monkeypatch.setattr(cvdocs, "APPS_DIR", tmp_path / "applications")  # never the real documents
    monkeypatch.setattr(users, "PATH", tmp_path / "users.json")  # never the real accounts


from jobalerts import dashboard as _dashboard  # noqa: E402

REAL_LIST_TASKS = _dashboard.list_tasks  # for the test of its parsing, with a fake PowerShell


@pytest.fixture(autouse=True)
def no_windows_tasks(monkeypatch):
    """Never read or change this computer's scheduled tasks."""
    from jobalerts import dashboard

    def blocked(*a, **k):
        raise AssertionError("a test tried to run PowerShell")
    monkeypatch.setattr(dashboard, "powershell", blocked)
    monkeypatch.setattr(dashboard, "list_tasks", lambda: [])
    monkeypatch.setattr(dashboard, "alert_owner", lambda text: None)  # never a real Telegram message
    monkeypatch.setattr(dashboard, "reload_settings", lambda: None)  # would load the real .env into the tests


@pytest.fixture
def con(tmp_path, monkeypatch):
    """A fresh SQLite database in a temp folder."""
    path = tmp_path / "jobs.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "DB_PATH", path)
    c = db.connect()
    yield c
    c.close()


def add_ad(con, uuid="nav-1", profile="p1", **kw):
    """An ad, handed to `profile` (None: to no profile). Sorting fields (relevant, score, summary,
    norwegian_required, triaged_at, sent_at) go to that profile's match."""
    match_keys = ("relevant", "score", "summary", "norwegian_required", "triaged_at", "sent_at")
    match = {k: kw.pop(k) for k in match_keys if k in kw}
    ad = dict(uuid=uuid, date_modified=None, title="Account Executive", employer="Acme AS",
              municipal="OSLO", url="https://example.org/ad", application_url=None,
              description="Sell B2B software.", source="nav")
    ad.update(kw)
    db.insert_ad(con, ad)
    if profile:
        db.add_match(con, profile, uuid)
        if match:
            sets = ", ".join(f"{k}=?" for k in match)
            con.execute(f"UPDATE matches SET {sets} WHERE profile_id=? AND uuid=?", (*match.values(), profile, uuid))
            con.commit()
    return ad
