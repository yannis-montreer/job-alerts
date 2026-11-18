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
}
os.environ.update(TEST_ENV)

from jobalerts import config, db  # noqa: E402


@pytest.fixture
def con(tmp_path, monkeypatch):
    """A fresh SQLite database in a temp folder."""
    path = tmp_path / "jobs.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "DB_PATH", path)
    c = db.connect()
    yield c
    c.close()


def add_ad(con, uuid="nav-1", **kw):
    ad = dict(uuid=uuid, date_modified=None, title="Account Executive", employer="Acme AS",
              municipal="OSLO", url="https://example.org/ad", application_url=None,
              description="Sell B2B software.", source="nav")
    ad.update(kw)
    db.insert_ad(con, ad)
    return ad
