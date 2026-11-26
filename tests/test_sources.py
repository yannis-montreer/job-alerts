from datetime import datetime, timedelta, timezone

import pytest

from jobalerts import db, finn, nav

ALERT = """Nye treff for søket ditt

Business Developer
Acme AS, Oslo
Flere detaljer: https://www.finn.no/123456789?utm_source=agent

Key Account Manager
Fjord Tech, Nes i Ådal
Flere detaljer: https://www.finn.no/job/ad/987654321

Selger
Uten Komma AS
Flere detaljer: https://www.finn.no/555
"""


def test_parse_alert_text():
    ads = finn.parse_alert_text(ALERT)
    assert [a["code"] for a in ads] == ["123456789", "987654321", "555"]
    assert ads[0] == dict(code="123456789", title="Business Developer", employer="Acme AS", location="Oslo")
    assert ads[1]["location"] == "Nes i Ådal"
    assert ads[2]["employer"] == "Uten Komma AS" and ads[2]["location"] == ""


@pytest.mark.parametrize("loc, expected", [
    ("Oslo", True), ("Lørenskog", True), ("Ås", True), ("", True),
    ("Oslo sentrum", True), ("Nes på Romerike", True), ("Akershus", True), ("Bærum, Oslo", True),
    ("Nes i Ådal", False), ("Mo i Rana", False), ("Bergen", False),
    ("Lierstranda", False), ("Osloveien", False), ("Ås i Trøndelag", False),
])
def test_finn_in_target(loc, expected):
    assert finn.in_target(loc) is expected


PAGE = """<html><head>
<script type="application/ld+json">{"@type": "BreadcrumbList"}</script>
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [{"@type": "Organization"},
 {"@type": "JobPosting", "title": "Business Developer",
  "description": "<p>We are looking for a <b>Business Developer</b> to grow B2B sales in Oslo.</p><ul><li>English required</li></ul>"}]}
</script></head><body>...</body></html>"""


def test_finn_disabled_by_default():
    assert finn.config.FINN_FETCH_PAGES is False


def test_extract_description_from_json_ld():
    text = finn.extract_description(PAGE)
    assert text.startswith("We are looking for a Business Developer to grow B2B sales in Oslo.")
    assert "- English required" in text
    assert finn.extract_description("<html>no structured data</html>") is None


def test_extract_description_finn_wrapper():
    # Real Finn pages wrap the JobPosting in a "script:ld+json" key.
    page = ('<script type="application/ld+json">{"script:ld+json": {"@context": "https://schema.org", '
            '"@type": "JobPosting", "description": "<p>Selge B2B-løsninger til bedriftskunder i hele Østlandet.</p>"}}</script>')
    assert finn.extract_description(page) == "Selge B2B-løsninger til bedriftskunder i hele Østlandet."


class FakeSession:
    def __init__(self):
        self.headers, self.urls = {}, []

    def get(self, url, timeout):
        self.urls.append(url)
        page = PAGE if url.endswith("/1") else "<html>nothing</html>"
        return type("R", (), {"text": page, "raise_for_status": lambda self: None})()


def test_fetch_descriptions_is_slow_capped_and_identified(con, monkeypatch):
    from conftest import add_ad
    session, sleeps = FakeSession(), []
    monkeypatch.setattr(finn.requests, "Session", lambda: session)
    monkeypatch.setattr(finn.time, "sleep", sleeps.append)
    monkeypatch.setattr(finn.config, "FINN_CONTACT_EMAIL", "me@example.org")
    monkeypatch.setattr(finn.config, "FINN_MAX_PAGES_PER_RUN", 2)
    ads = []
    for code in ("1", "2", "3"):
        ads.append(add_ad(con, f"finn:{code}", url=f"https://www.finn.no/{code}", source="finn",
                          description=finn.LIMITED_INFO))
    stats = finn.fetch_descriptions(con, ads)
    assert stats == dict(fetched=1, failed=1, skipped=1)
    # final address directly (no redirect), one request per ad; cap: third not fetched
    assert session.urls == ["https://www.finn.no/job/ad/1", "https://www.finn.no/job/ad/2"]
    assert sleeps == [finn.config.FINN_DELAY_S] and finn.config.FINN_DELAY_S >= 5
    assert "me@example.org" in session.headers["User-Agent"]
    rows = {r["uuid"]: r["description"] for r in con.execute("SELECT uuid, description FROM ads")}
    assert rows["finn:1"].startswith("We are looking for")
    assert rows["finn:2"] == rows["finn:3"] == finn.LIMITED_INFO  # failures keep the preview


def test_fetch_requires_contact_email(monkeypatch):
    monkeypatch.setattr(finn.config, "FINN_CONTACT_EMAIL", "")
    with pytest.raises(RuntimeError, match="FINN_CONTACT_EMAIL"):
        finn.user_agent()


def test_purge_deletes_finn_content_only(con):
    from conftest import add_ad
    add_ad(con, "finn:9", source="finn", description="Full text")
    add_ad(con, "nav-9", description="NAV text")
    con.execute("UPDATE ads SET summary_fr='summary'")
    con.commit()
    assert finn.purge(con) == 1
    rows = {r["uuid"]: (r["description"], r["summary_fr"]) for r in con.execute("SELECT * FROM ads")}
    assert rows["finn:9"] == (finn.LIMITED_INFO, None)
    assert rows["nav-9"] == ("NAV text", "summary")


def test_html_to_text():
    html = "<p>Hello&nbsp;<b>world</b></p><ul><li>one</li><li>two</li></ul>"
    assert nav.html_to_text(html) == "Hello\xa0world\n- one\n- two"


def test_nav_location_filters():
    assert nav.is_target_location({"municipal": "OSLO"})
    assert nav.is_target_location({"municipal": "?"})  # unknown: decided from the detail
    assert not nav.is_target_location({"municipal": "BERGEN"})
    assert nav.detail_in_target({"workLocations": [{"municipal": "Bergen", "county": "Akershus"}]})
    assert not nav.detail_in_target({"workLocations": [{"municipal": "Bergen", "county": "Vestland"}]})


def test_compute_since_window(con):
    now = datetime.now(timezone.utc)
    since = nav.compute_since(con)
    assert timedelta(hours=23.9) < now - since < timedelta(hours=24.1)
    # missed runs: go back to the last success (minus 1 h), capped at 7 days
    db.set_meta(con, "nav_last_success", (now - timedelta(days=2)).isoformat())
    assert timedelta(hours=48.9) < now - nav.compute_since(con) < timedelta(hours=49.1)
    db.set_meta(con, "nav_last_success", (now - timedelta(days=30)).isoformat())
    assert now - nav.compute_since(con) < timedelta(days=7, minutes=1)
