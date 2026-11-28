"""CV and cover letter PDFs adapted to each kept ad.

Per profile, in profiles/<id>/ (git-ignored):
- cv_original.pdf or .docx   the CV uploaded in the dashboard (the source of facts)
- cv_text.txt                its text
- cv_base.json               its facts, structured once by the AI (CV_SCHEMA)
- photo.png / photo.jpg      the photo found in the PDF, if any
- cv_template.html           layout copying the uploaded CV (Jinja2, written by the AI from the PDF)
- letter_template.html       cover letter in the same style
Without templates, the default ones in jobalerts/templates/ are used.

For a kept ad: the AI adapts cv_base.json to the job (English, facts only), the CV and the
letter are rendered into the templates, and Edge (headless, scripts off) prints them as PDFs in
data/applications/<profile>/<ad>/.
"""
import base64
import html as htmllib
import json
import logging
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

from jinja2.sandbox import SandboxedEnvironment

from . import config, profiles, triage

log = logging.getLogger(__name__)
DEFAULT_TEMPLATES = Path(__file__).with_name("templates")
APPS_DIR = config.ROOT / "data" / "applications"
MAX_UPLOAD = 10 * 1024 * 1024
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]

_item = {"type": "object", "properties": {
    "title": {"type": "string"}, "organisation": {"type": "string"}, "location": {"type": "string"},
    "start": {"type": "string"}, "end": {"type": "string"},
    "bullets": {"type": "array", "items": {"type": "string"}}},
    "required": ["title", "organisation", "location", "start", "end", "bullets"]}
CV_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"}, "headline": {"type": "string"},
        "email": {"type": "string"}, "phone": {"type": "string"}, "location": {"type": "string"},
        "links": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "experience": {"type": "array", "items": _item},
        "education": {"type": "array", "items": _item},
        "languages": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "level": {"type": "string"}}, "required": ["name", "level"]}},
        "skills": {"type": "array", "items": {"type": "string"}},
        "interests": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "headline", "email", "phone", "location", "links", "summary", "experience",
                 "education", "languages", "skills", "interests"],
}
TEMPLATE_SCHEMA = {"type": "object", "properties": {"cv_html": {"type": "string"}, "letter_html": {"type": "string"}},
                   "required": ["cv_html", "letter_html"]}

TEMPLATE_RULES = """Template rules (Jinja2, rendered offline then printed to PDF by a browser, A4):
- One complete HTML document with all CSS in a <style> element. No JavaScript, no external images.
  Fonts: system fonts, or Google Fonts through a <link> to fonts.googleapis.com only.
- @page { size: A4; margin: 0 } and print-friendly CSS (print-color-adjust: exact).
- CV template variables: cv.name, cv.headline, cv.email, cv.phone, cv.location, cv.links (list),
  cv.summary, cv.experience and cv.education (lists of items with .title .organisation .location
  .start .end .bullets), cv.languages (list with .name .level), cv.skills (list), cv.interests (list),
  and photo (a data: URL, or empty: then show no photo and no empty circle).
  Loop with {% for x in cv.experience %}...{% endfor %}, test with {% if cv.interests %}.
  Every list may be empty or longer than in the original: the layout must cope (hide empty sections).
- Letter template variables: the same cv and photo (for the header), and letter.date, letter.employer,
  letter.job_title, letter.paragraphs (list of paragraphs: greeting, body, closing and signature).
- Never write personal data into the template itself: only variables."""


def folder(profile=None) -> Path:
    p = profile or profiles.current()
    return profiles.PROFILES_DIR / p.id


def original(profile=None) -> Path | None:
    for name in ("cv_original.pdf", "cv_original.docx"):
        path = folder(profile) / name
        if path.exists():
            return path
    return None


def base(profile=None) -> dict | None:
    path = folder(profile) / "cv_base.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def photo_uri(profile=None) -> str:
    for name, mime in (("photo.png", "image/png"), ("photo.jpg", "image/jpeg")):
        path = folder(profile) / name
        if path.exists():
            return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()
    return ""


def has_custom_template(profile=None) -> bool:
    return (folder(profile) / "cv_template.html").exists()


def ready(profile=None) -> bool:
    """A CV is uploaded and its facts are extracted: adapted PDFs can be made."""
    return base(profile) is not None


def state(profile=None) -> dict:
    src = original(profile)
    b = base(profile)
    meta = folder(profile) / "cv_meta.json"
    m = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
    return dict(uploaded=bool(src), filename=m.get("filename", src.name if src else ""), uploaded_at=m.get("uploaded_at", ""),
                kind=src.suffix[1:] if src else "", facts=bool(b), name=(b or {}).get("name", ""),
                jobs=len((b or {}).get("experience", [])), photo=bool(photo_uri(profile)),
                template="custom" if has_custom_template(profile) else "default", edge=bool(edge_path()))


# ---------- upload ----------

def extract_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages).strip()
    import docx
    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(x for x in parts if x.strip())


def extract_photo(path: Path, dest: Path) -> bool:
    """Largest image of the first page (a CV photo), if any."""
    for old in ("photo.png", "photo.jpg"):
        (dest / old).unlink(missing_ok=True)
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        try:
            images = list(PdfReader(str(path)).pages[0].images)
        except Exception as e:
            log.info("No photo read from the CV: %s", e)
            return False
        images = [i for i in images if len(i.data) > 5000]
        if not images:
            return False
        img = max(images, key=lambda i: len(i.data))
        ext = "png" if img.name.lower().endswith(".png") else "jpg"
        try:  # normalise through Pillow when available (pypdf decodes to it)
            img.image.convert("RGB").save(dest / f"photo.{ext}")
        except Exception:
            (dest / f"photo.{ext}").write_bytes(img.data)
        return True
    import docx
    d = docx.Document(str(path))
    blobs = [r.target_part.blob for r in d.part.rels.values() if "image" in r.reltype]
    if not blobs:
        return False
    data = max(blobs, key=len)
    (dest / ("photo.png" if data[:4] == b"\x89PNG" else "photo.jpg")).write_bytes(data)
    return True


def save_upload(filename: str, data: bytes, profile=None) -> dict:
    """Store an uploaded CV for the profile and read its text and photo. The AI steps come after."""
    ext = Path(filename).suffix.lower()
    if ext not in (".pdf", ".docx"):
        raise ValueError("Upload a PDF or a Word (.docx) file")
    if len(data) > MAX_UPLOAD:
        raise ValueError("The file is larger than 10 MB")
    if ext == ".pdf" and not data.startswith(b"%PDF"):
        raise ValueError("This file is not a PDF")
    if ext == ".docx" and not data.startswith(b"PK"):
        raise ValueError("This file is not a Word .docx document")
    dest = folder(profile)
    dest.mkdir(parents=True, exist_ok=True)
    for old in ("cv_original.pdf", "cv_original.docx", "cv_base.json"):
        (dest / old).unlink(missing_ok=True)
    path = dest / f"cv_original{ext}"
    path.write_bytes(data)
    text = extract_text(path)
    if len(text) < 200:
        path.unlink()
        raise ValueError("Almost no text could be read from this file (a scanned image?): upload a text PDF or a Word file")
    (dest / "cv_text.txt").write_text(text, encoding="utf-8")
    photo = extract_photo(path, dest)
    from .db import now_iso
    (dest / "cv_meta.json").write_text(json.dumps(dict(filename=Path(filename).name[:120], uploaded_at=now_iso())),
                                      encoding="utf-8")
    return dict(chars=len(text), photo=photo)


# ---------- AI: facts and layout ----------

FACTS_PROMPT = """Below is the text of a CV. Turn it into structured data (the JSON schema), copying the facts
exactly: names, organisations, places, dates, job titles, bullet points, languages and levels, skills,
interests. Keep the original wording of each bullet; translate nothing; invent nothing; leave a field
empty when the CV does not say it. Put trainings and degrees in education. Order: as in the CV.

# CV TEXT
{text}"""


def build_facts(profile=None) -> dict:
    dest = folder(profile)
    text = (dest / "cv_text.txt").read_text(encoding="utf-8")
    src = original(profile)
    pdf = src if src and src.suffix.lower() == ".pdf" else None  # the PDF shows the real reading order
    prompt = FACTS_PROMPT.format(text=text[:20000])
    if pdf:
        prompt += "\n\nThe extracted text above may be out of order: follow the order and grouping of the PDF itself."
    facts = triage.ai_json(prompt, CV_SCHEMA, pdf=pdf)
    (dest / "cv_base.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    return facts


TEMPLATE_PROMPT = """Look carefully at the CV in the PDF document: its layout, colours, fonts, sizes, spacing,
columns, section order, the shape and place of the photo, the icons and lines.
Write two Jinja2 HTML templates that reproduce this design as faithfully as you can in HTML/CSS:
cv_html for the CV, and letter_html for a one-page cover letter in the same style (same header
with the name, headline, contact line and photo, then the letter).

{rules}"""


_DANGEROUS = re.compile(r"<\s*(script|iframe|object|embed|base|meta\s+http-equiv)[^>]*>.*?(<\s*/\s*\1\s*>|$)",
                        re.I | re.S)


def sanitize(template: str) -> str:
    """No scripts, frames, event handlers or remote resources other than Google Fonts."""
    t = _DANGEROUS.sub("", template)
    t = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", t, flags=re.I)
    t = re.sub(r"(src|href)\s*=\s*([\"'])(https?:)?//(?!fonts\.(googleapis|gstatic)\.com)[^\"']*\2", r'\1=""', t, flags=re.I)
    t = re.sub(r"url\(\s*([\"']?)(https?:)?//(?!fonts\.(googleapis|gstatic)\.com)[^)]*\)", "url()", t, flags=re.I)
    return t


def build_template(profile=None) -> None:
    """The AI looks at the uploaded PDF and writes templates copying its layout."""
    src = original(profile)
    if src is None or src.suffix.lower() != ".pdf":
        raise ValueError("The layout can only be copied from a PDF CV (with a Word file, the default layout is used)")
    out = triage.ai_json(TEMPLATE_PROMPT.format(rules=TEMPLATE_RULES), TEMPLATE_SCHEMA, pdf=src)
    cv_html, letter_html = sanitize(out["cv_html"]), sanitize(out["letter_html"])
    sample = base(profile) or SAMPLE_CV
    render(cv_html, cv=sample, photo="")  # must render: Jinja errors are caught here, not at the next Keep
    render(letter_html, cv=sample, photo="", letter=sample_letter(sample))
    dest = folder(profile)
    (dest / "cv_template.html").write_text(cv_html, encoding="utf-8")
    (dest / "letter_template.html").write_text(letter_html, encoding="utf-8")


def reset_template(profile=None):
    for name in ("cv_template.html", "letter_template.html"):
        (folder(profile) / name).unlink(missing_ok=True)


# ---------- adapting to an ad ----------

TAILOR_PROMPT = """Adapt {name}'s CV to the job ad below, to maximise the chances of an interview.
Use ONLY facts from the CV data: never invent an employer, title, date, number, degree, skill or language.
You may: write a new headline and summary aimed at this job and this employer (2 to 4 sentences);
reorder and rephrase bullet points to highlight what the ad asks for; drop or shorten what is
irrelevant; reorder skills (only skills supported by the CV). Keep every job and its dates; keep
experience in reverse chronological order. Remove any sentence aimed at another employer.
Write everything in English. Keep it about as long as the original CV (it must stay at one page if
the original is one page).

# CV DATA (facts)
{base}

# JOB AD
Title: {title}
Employer: {employer}
Place: {place}

{description}"""


def tailor(ad, profile=None) -> dict:
    b = base(profile)
    p = profile or profiles.current()
    return triage.ai_json(TAILOR_PROMPT.format(
        name=p.candidate_name, base=json.dumps(b, ensure_ascii=False, indent=1),
        title=ad["title"] or "", employer=ad["employer"] or "", place=ad["municipal"] or "",
        description=(ad["description"] or "")[:6000]), CV_SCHEMA)


# ---------- rendering ----------

_env = SandboxedEnvironment(autoescape=True)


def template_text(kind: str, profile=None) -> str:
    custom = folder(profile) / f"{kind}_template.html"
    return (custom if custom.exists() else DEFAULT_TEMPLATES / f"{kind}_default.html").read_text(encoding="utf-8")


def render(template: str, **data) -> str:
    return _env.from_string(template).render(**data)


def edge_path() -> str | None:
    for p in EDGE_PATHS:
        if Path(p).exists():
            return p
    return shutil.which("msedge") or shutil.which("chrome")


def html_to_pdf(html: str, pdf: Path):
    exe = edge_path()
    if not exe:
        raise RuntimeError("Microsoft Edge (or Chrome) is needed to make PDFs")
    pdf.parent.mkdir(parents=True, exist_ok=True)
    src = pdf.with_suffix(".html")
    # No script may run, and nothing but Google Fonts and inline images may load.
    csp = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'none'; "
           "style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com data:; "
           "img-src data:\">")
    if re.search(r"<head[^>]*>", html, re.I):
        html = re.sub(r"<head[^>]*>", lambda m: m.group(0) + csp, html, count=1, flags=re.I)
    else:
        html = csp + html
    src.write_text(html, encoding="utf-8")
    pdf.unlink(missing_ok=True)
    profile_dir = config.ROOT / "data" / "pdf-browser"
    proc = subprocess.run(
        [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         "--no-pdf-header-footer", f"--user-data-dir={profile_dir}",
         f"--print-to-pdf={pdf}", src.resolve().as_uri()],
        capture_output=True, text=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not pdf.exists() or pdf.stat().st_size < 1000:
        raise RuntimeError(f"The PDF could not be made: {(proc.stderr or proc.stdout).strip()[-300:]}")


def page_count(pdf: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(pdf)).pages)


def target_pages(profile=None) -> int:
    src = original(profile)
    try:
        return page_count(src) if src and src.suffix.lower() == ".pdf" else 2
    except Exception:
        return 2


def pdf_fitted(html: str, pdf: Path, pages: int):
    """Print, then print again slightly smaller while it has more pages than the original CV."""
    for zoom in (1.0, 0.95, 0.9, 0.85, 0.8):
        sized = html if zoom == 1.0 else re.sub(r"</head>", f"<style>html{{zoom:{zoom}}}</style></head>", html, count=1, flags=re.I)
        html_to_pdf(sized, pdf)
        if page_count(pdf) <= pages:
            return


def safe_name(text: str) -> str:
    return re.sub(r"[^\w .,()&-]+", "", text, flags=re.UNICODE).strip()[:60] or "Job"


def sample_letter(cv: dict) -> dict:
    return dict(date=date.today().strftime("%d %B %Y"), employer="Employer AS", job_title="Job title",
                paragraphs=["Dear Hiring Manager,", "This is where the letter goes.", "Best regards,", cv.get("name", "")])


def letter_data(ad, letter_text: str) -> dict:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", letter_text.strip()) if p.strip()]
    return dict(date=date.today().strftime("%d %B %Y"), employer=ad["employer"] or "", job_title=ad["title"] or "",
                paragraphs=paragraphs)


def make_documents(ad, letter_text: str, profile=None) -> dict:
    """Adapted CV and letter PDFs for a kept ad. Returns {"cv": path, "letter": path|None}."""
    p = profile or profiles.current()
    out = APPS_DIR / p.id / safe_name(ad["uuid"].replace(":", "-"))
    cv = tailor(ad, p)
    (out.parent).mkdir(parents=True, exist_ok=True)
    out.mkdir(exist_ok=True)
    (out / "cv.json").write_text(json.dumps(cv, ensure_ascii=False, indent=1), encoding="utf-8")
    photo = photo_uri(p)
    who = safe_name(cv.get("name") or p.candidate_name)
    company = safe_name(ad["employer"] or ad["title"] or "")
    files = {}
    cv_pdf = out / f"CV - {who} - {company}.pdf"
    pdf_fitted(render(template_text("cv", p), cv=cv, photo=photo), cv_pdf, target_pages(p))
    files["cv"] = cv_pdf
    if letter_text.strip():
        letter_pdf = out / f"Cover letter - {who} - {company}.pdf"
        pdf_fitted(render(template_text("letter", p), cv=cv, photo=photo, letter=letter_data(ad, letter_text)), letter_pdf, 1)
        files["letter"] = letter_pdf
    return files


def preview(profile=None) -> Path:
    """The uploaded CV's facts in the current layout (to check the template)."""
    p = profile or profiles.current()
    b = base(p)
    if b is None:
        raise ValueError("Upload a CV first")
    pdf = folder(p) / "preview" / "CV preview.pdf"
    pdf_fitted(render(template_text("cv", p), cv=b, photo=photo_uri(p)), pdf, target_pages(p))
    return pdf


def documents(profile=None, uuid: str = "") -> list[Path]:
    p = profile or profiles.current()
    d = APPS_DIR / p.id / safe_name(uuid.replace(":", "-"))
    return sorted(d.glob("*.pdf"), key=lambda f: (not f.name.startswith("CV"), f.name)) if d.exists() else []


SAMPLE_CV = dict(name="Alex Example", headline="Sales Executive", email="alex@example.com", phone="+47 000 00 000",
                 location="Oslo", links=[], summary="Sample summary.",
                 experience=[dict(title="Account Executive", organisation="Acme AS", location="Oslo", start="2023",
                                  end="Present", bullets=["Closed deals.", "Built pipeline."])],
                 education=[dict(title="Bachelor", organisation="University", location="Oslo", start="2018",
                                 end="2021", bullets=[])],
                 languages=[dict(name="English", level="Fluent")], skills=["Prospecting"], interests=["Hiking"])


def escape(text: str) -> str:
    return htmllib.escape(text)
