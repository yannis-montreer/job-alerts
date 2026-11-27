import json

import pytest
from conftest import add_ad

from jobalerts import config, linkedin, preferences, telegram, triage

# Usual plain-text layout of a LinkedIn job alert (synthetic sample).
ALERT = """Your job alert for business developer in Oslo, Norway
3 new jobs match your preferences.

Business Developer
Acme Software AS
Oslo, Oslo, Norway
Actively recruiting
View job: https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc%3D%3D&refId=x

---------------------------------------------------------

Sales Development Representative
Fjord Tech
Bærum, Akershus, Norway (Hybrid)
Easy Apply
View job: https://www.linkedin.com/comm/jobs/view/4087654321/?trackingId=def

---------------------------------------------------------

Account Executive
Nordic SaaS
Norway (Remote)
2 connections work here
View job: https://www.linkedin.com/jobs/view/4011112222/

See all jobs: https://www.linkedin.com/comm/jobs/search/?keywords=business
Unsubscribe: https://www.linkedin.com/comm/psettings/email-unsubscribe
"""


def test_parse_linkedin_alert():
    ads = linkedin.parse_alert_text(ALERT)
    assert [a["id"] for a in ads] == ["4012345678", "4087654321", "4011112222"]
    assert ads[0] == dict(id="4012345678", title="Business Developer", company="Acme Software AS",
                          location="Oslo, Oslo, Norway")
    assert ads[1]["title"] == "Sales Development Representative" and ads[1]["company"] == "Fjord Tech"
    assert ads[2]["company"] == "Nordic SaaS"


@pytest.mark.parametrize("raw, clean, kept", [
    ("Oslo, Oslo, Norway", "Oslo, Oslo", True),
    ("Bærum, Akershus, Norway (Hybrid)", "Bærum, Akershus", True),
    ("Norway (Remote)", "", True),          # country-wide: unknown, kept
    ("Bergen, Vestland, Norway", "Bergen, Vestland", False),
])
def test_linkedin_location(raw, clean, kept):
    assert linkedin.clean_location(raw) == clean
    assert linkedin.in_target(clean) is kept


def test_linkedin_fetch_new_ads(con, monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LINKEDIN", True)
    monkeypatch.setattr(linkedin.mail, "credentials", lambda: ("a@b.c", "pw"))
    monkeypatch.setattr(linkedin.mail, "alert_texts", lambda sender, box, since: [ALERT])
    new, stats = linkedin.fetch_new_ads(con)
    assert stats["new"] == 3 and {a["source"] for a in new} == {"linkedin"}
    row = con.execute("SELECT * FROM ads WHERE uuid='linkedin:4012345678'").fetchone()
    assert row["url"] == "https://www.linkedin.com/jobs/view/4012345678/"
    assert row["description"] == linkedin.LIMITED_INFO
    assert linkedin.fetch_new_ads(con)[1]["seen"] == 3  # never added twice


def test_linkedin_off_by_default(con, monkeypatch):
    monkeypatch.setattr(linkedin.mail, "alert_texts", lambda *a: pytest.fail("should not read mail"))
    assert config.SOURCE_LINKEDIN is False
    assert linkedin.fetch_new_ads(con) == ([], {})


def test_linkedin_limited_preview_in_telegram():
    ad = dict(title="t", employer="e", municipal="OSLO", score=4, norwegian_required=0, source="linkedin",
              summary="s", url="https://www.linkedin.com/jobs/view/1/", description=linkedin.LIMITED_INFO)
    assert "LinkedIn: limited preview" in telegram.format_offer(ad)


def test_preferences_clean_and_prompt():
    data = preferences.save({
        "target_roles": [{"name": " Barista ", "level": "also"}, {"name": "barista", "level": "main"},
                         {"name": "Business Developer", "level": "weird"}, {"name": ""}],
        "exclude": ["Call center", "call center", "  "],
    })
    assert data["target_roles"] == [{"name": "Barista", "level": "also"}, {"name": "Business Developer", "level": "main"}]
    assert data["exclude"] == ["Call center"]
    assert json.loads(preferences.PATH.read_text(encoding="utf-8")) == data
    section = preferences.prompt_section()
    assert "Main target roles" in section and "Business Developer" in section
    assert "Also acceptable roles" in section and "Barista" in section and "score 3 at most" in section
    assert "Always reject (relevant = false): Call center" in section


def test_no_preferences_no_prompt_section():
    assert preferences.load() == preferences.empty()
    assert preferences.prompt_section() == ""


def test_preferences_reach_the_sorting_prompt(con, tmp_path, monkeypatch):
    profile = tmp_path / "profile.md"
    profile.write_text("Profile text", encoding="utf-8")
    monkeypatch.setattr(config, "PROFILE_PATH", profile)
    preferences.save({"target_roles": [{"name": "Barista", "level": "also"}], "exclude": []})
    add_ad(con, "x")
    prompt = triage.build_prompt(con, con.execute("SELECT * FROM ads").fetchall())
    assert prompt.index("# PROFILE") < prompt.index("# PREFERENCES") < prompt.index("# PAST DECISIONS")
    assert "Barista" in prompt
