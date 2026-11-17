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
