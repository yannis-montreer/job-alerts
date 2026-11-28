"""CV upload, layout templates, adapted CV and letter PDFs sent after a Keep."""
import io
import json

import pytest

from conftest import add_ad
from jobalerts import cvdocs, kit, profiles, telegram, triage

FACTS = dict(cvdocs.SAMPLE_CV, name="Alex Example")


def docx_bytes(text: str) -> bytes:
    import docx
    d = docx.Document()
    for line in text.split("\n"):
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_upload_checks_the_file():
    with pytest.raises(ValueError, match="PDF or a Word"):
        cvdocs.save_upload("cv.txt", b"hello")
    with pytest.raises(ValueError, match="not a PDF"):
        cvdocs.save_upload("cv.pdf", b"PK fake")
    with pytest.raises(ValueError, match="Almost no text"):
        cvdocs.save_upload("cv.docx", docx_bytes("Short"))


def test_word_upload_is_read_and_kept_per_profile():
    text = "Alex Example\nAccount Executive at Acme AS, Oslo, 2023 - present\n" + "Sold B2B software to SMEs. " * 12
    info = cvdocs.save_upload("My CV.docx", docx_bytes(text))
    assert info["chars"] > 200 and info["photo"] is False
    st = cvdocs.state()
    assert (st["uploaded"], st["filename"], st["kind"], st["facts"], st["template"]) == (True, "My CV.docx", "docx", False, "default")
    assert "Account Executive at Acme AS" in (cvdocs.folder() / "cv_text.txt").read_text(encoding="utf-8")
    assert cvdocs.folder().parent == profiles.PROFILES_DIR  # never the real profiles folder in tests


def test_facts_are_stored_and_make_the_profile_ready(monkeypatch):
    cvdocs.save_upload("cv.docx", docx_bytes("Alex Example, sales. " * 20))
    seen = {}
    monkeypatch.setattr(triage, "ai_json", lambda prompt, schema, pdf=None: seen.update(pdf=pdf, prompt=prompt) or FACTS)
    assert not cvdocs.ready()
    cvdocs.build_facts()
    assert cvdocs.ready() and cvdocs.base()["name"] == "Alex Example"
    assert seen["pdf"] is None and "Alex Example, sales." in seen["prompt"]  # a Word file: text only


def test_layout_needs_a_pdf():
    cvdocs.save_upload("cv.docx", docx_bytes("Alex Example, sales. " * 20))
    with pytest.raises(ValueError, match="only be copied from a PDF"):
        cvdocs.build_template()


def test_templates_are_cleaned_and_sandboxed():
    dirty = ('<html><head><script>alert(1)</script><link href="https://evil.example/x.css" rel="stylesheet">'
             '<link href="https://fonts.googleapis.com/css2?family=Raleway" rel="stylesheet"></head>'
             '<body onload="steal()"><img src="https://evil.example/p.png">'
             '<div style="background:url(https://evil.example/b.png)">{{ cv.name }}</div></body></html>')
    clean = cvdocs.sanitize(dirty)
    assert "<script" not in clean and "onload" not in clean and "evil.example" not in clean
    assert "fonts.googleapis.com" in clean
    out = cvdocs.render(clean, cv={"name": "<b>Alex</b>"})
    assert "&lt;b&gt;Alex&lt;/b&gt;" in out  # data is escaped
    with pytest.raises(Exception):  # the sandbox refuses Python internals
        cvdocs.render("{{ cv.__class__.__mro__ }}{{ ''.__class__.__subclasses__() }}", cv={})


def test_letter_paragraphs_and_default_templates_render():
    data = cvdocs.letter_data({"employer": "Acme AS", "title": "SDR"}, "Dear Kari,\n\nI apply.\n\nBest regards,\nAlex")
    assert data["paragraphs"] == ["Dear Kari,", "I apply.", "Best regards,\nAlex"]
    cv = cvdocs.render(cvdocs.template_text("cv"), cv=FACTS, photo="")
    letter = cvdocs.render(cvdocs.template_text("letter"), cv=FACTS, photo="", letter=data)
    assert "Alex Example" in cv and "Account Executive" in cv and "<img" not in cv
    assert "Application – SDR" in letter and "I apply." in letter


@pytest.fixture
def kept(con, tmp_path, monkeypatch):
    profile = tmp_path / "profile.md"
    profile.write_text("Alex: B2B sales.", encoding="utf-8")
    from jobalerts import config
    monkeypatch.setattr(config, "PROFILE_PATH", profile)
    add_ad(con, "a")
    con.execute("INSERT INTO messages(bot_id, message_id, profile_id, uuid, sent_at) VALUES('b1', 7, 'p1', 'a', 'x')")
    con.execute("INSERT INTO decisions(profile_id, uuid, decision, decided_at, telegram_update_id) VALUES('p1','a','keep','x',1)")
    con.commit()
    monkeypatch.setattr(triage, "ai_json", lambda prompt, schema, pdf=None: {
        "deadline": "", "contact": "", "how_to_apply": "Form.", "requirements": [], "gaps": [],
        "talking_points": [], "cover_letter": "Dear team,\n\nI apply.\n\nAlex"})
    calls = []
    monkeypatch.setattr(telegram, "api", lambda method, _timeout=30, bot=None, **p: calls.append((method, p)) or {"message_id": 9})
    monkeypatch.setattr(telegram, "send_document", lambda path, caption="", bot=None, reply=None:
                        calls.append(("sendDocument", {"name": path.name, "reply": reply})))
    return calls


def test_without_cv_only_the_text_kit_is_sent(con, kept):
    assert kit.send_kits(con) == 1
    assert [m for m, _ in kept] == ["sendMessage", "sendMessage"] and kit.pending(con) == []


def test_with_a_cv_the_pdfs_follow_the_kit(con, kept, monkeypatch):
    (cvdocs.folder()).mkdir(parents=True, exist_ok=True)
    (cvdocs.folder() / "cv_base.json").write_text(json.dumps(FACTS), encoding="utf-8")
    made = []

    def make(ad, letter, p):
        made.append(letter)
        out = cvdocs.APPS_DIR / p.id / "a"
        out.mkdir(parents=True, exist_ok=True)
        (out / "CV - Alex - Acme AS.pdf").write_bytes(b"%PDF cv")
        (out / "Cover letter - Alex - Acme AS.pdf").write_bytes(b"%PDF letter")
        return {"cv": out / "CV - Alex - Acme AS.pdf", "letter": out / "Cover letter - Alex - Acme AS.pdf"}
    monkeypatch.setattr(cvdocs, "APPS_DIR", cvdocs.folder().parent.parent / "apps")  # a temp folder
    monkeypatch.setattr(cvdocs, "make_documents", make)
    assert kit.send_kits(con) == 1
    docs = [p for m, p in kept if m == "sendDocument"]
    assert [d["name"] for d in docs] == ["CV - Alex - Acme AS.pdf", "Cover letter - Alex - Acme AS.pdf"]
    assert docs[0]["reply"]["message_id"] == 7 and made == ["Dear team,\n\nI apply.\n\nAlex"]
    assert kit.pending(con) == []
    assert [p.name for p in cvdocs.documents(uuid="a")] == ["CV - Alex - Acme AS.pdf", "Cover letter - Alex - Acme AS.pdf"]


def test_pdf_failures_are_retried_without_resending_the_text(con, kept, monkeypatch):
    (cvdocs.folder()).mkdir(parents=True, exist_ok=True)
    (cvdocs.folder() / "cv_base.json").write_text(json.dumps(FACTS), encoding="utf-8")

    def broken(ad, letter, p):
        raise RuntimeError("Edge missing")
    monkeypatch.setattr(cvdocs, "make_documents", broken)
    kit.send_kits(con)
    texts = [p for m, p in kept if m == "sendMessage"]
    assert len(texts) == 2  # the text kit
    for _ in range(kit.MAX_ATTEMPTS - 1):
        kit.send_kits(con)
    texts = [p["text"] for m, p in kept if m == "sendMessage"]
    assert len(texts) == 3 and "could not be made after 3 tries" in texts[-1]
    assert kit.pending(con) == []
