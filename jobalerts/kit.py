"""Application kit: sent on Telegram after a Keep click.

For each ad whose latest decision is "keep", one AI call prepares what is needed to apply:
deadline, contact, how to apply, key requirements, gaps, talking points and a draft cover
letter. The kit is sent as a reply to the ad's message, once. Runs after the clicks are
collected (morning run and catch-up runs). Failures are retried at the next runs.
"""
import html
import json
import logging

from . import cvdocs, db, preferences, profiles, telegram, triage
from .texts import language_name, t

log = logging.getLogger(__name__)
MAX_PER_RUN = 3      # each kit is one AI call (30-60 s); the catch-up task has 5 minutes
MAX_ATTEMPTS = 3
MAX_DESC_CHARS = 6000

KIT_SCHEMA = {
    "type": "object",
    "properties": {
        "deadline": {"type": "string"},
        "contact": {"type": "string"},
        "how_to_apply": {"type": "string"},
        "requirements": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "talking_points": {"type": "array", "items": {"type": "string"}},
        "cover_letter": {"type": "string"},
    },
    "required": ["deadline", "contact", "how_to_apply", "requirements", "gaps", "talking_points", "cover_letter"],
}

KIT_PROMPT = """{name} wants to apply to the job ad below. Prepare an application kit, using ONLY facts from the ad and from {name}'s profile. Never invent experience, numbers or names.

Write every field in {language}, except cover_letter (see below).
- deadline: the application deadline as written in the ad (date, "ASAP", "rolling"...), or "" if the ad does not say.
- contact: the contact person(s) with role, phone and email as written in the ad, or "".
- how_to_apply: one sentence: how and where to apply (form, email, link) according to the ad.
- requirements: the 3 to 6 key requirements of the ad, short.
- gaps: requirements the profile does not clearly meet, short and honest (empty list if none).
- talking_points: 3 to 5 concrete points from the profile that match this job, to use in the letter or the interview.
- cover_letter: a draft cover letter of 150 to 220 words, first person, signed {name}. Write it in English, or in French if the ad is in French; never in Norwegian. Use [brackets] for anything to complete by hand.
If the ad text is only a title (limited preview, no description), say in how_to_apply that the full ad must be read first, leave requirements and gaps empty, and keep the letter general.

# PROFILE
{profile}
{preferences}
# JOB AD
Title: {title}
Employer: {employer}
Place: {place}
Link: {url}
Application link: {application_url}

{description}
"""


def pending(con) -> list:
    """The current profile's kept ads whose kit, or whose CV and letter PDFs (when a CV is
    uploaded), are not sent yet."""
    pid = profiles.current().id
    docs = 1 if cvdocs.ready() else 0
    return con.execute(
        """SELECT a.* FROM ads a
           JOIN decisions d ON d.uuid = a.uuid AND d.profile_id = ?
           LEFT JOIN kits k ON k.uuid = a.uuid AND k.profile_id = d.profile_id
           WHERE d.id = (SELECT MAX(id) FROM decisions WHERE uuid = a.uuid AND profile_id = d.profile_id)
             AND d.decision = 'keep'
             AND (k.uuid IS NULL OR (k.sent_at IS NULL AND k.attempts < ?)
                  OR (? AND k.sent_at IS NOT NULL AND k.docs_at IS NULL AND k.docs_attempts < ?))
           ORDER BY d.id""",
        (pid, MAX_ATTEMPTS, docs, MAX_ATTEMPTS),
    ).fetchall()


def build_prompt(ad) -> str:
    p = profiles.current()
    profile = p.profile_path.read_text(encoding="utf-8") if p.profile_path.exists() else "(no profile)"
    prefs = preferences.prompt_section()
    return KIT_PROMPT.format(
        name=p.candidate_name, language=language_name(), profile=profile,
        preferences=("\n" + prefs + "\n") if prefs else "",
        title=ad["title"] or "", employer=ad["employer"] or "", place=ad["municipal"] or "",
        url=ad["url"] or "", application_url=ad["application_url"] or "",
        description=(ad["description"] or "(no description: limited preview)")[:MAX_DESC_CHARS],
    )


def _items(values) -> str:
    return "\n".join(f"• {html.escape(str(v))}" for v in values if str(v).strip())


def format_kit(ad, kit: dict) -> list[str]:
    """Telegram HTML messages (the letter goes in its own message so it is easy to copy)."""
    e = html.escape
    lines = [t("kit_title", title=e(ad["title"] or ""), employer=e(ad["employer"] or ""))]
    if kit.get("deadline"):
        lines.append(t("kit_deadline", v=e(kit["deadline"])))
    if kit.get("contact"):
        lines.append(t("kit_contact", v=e(kit["contact"])))
    if kit.get("how_to_apply"):
        lines.append(t("kit_how", v=e(kit["how_to_apply"])))
    link = ad["application_url"] or ad["url"]
    if link:
        lines.append(f'<a href="{e(link, quote=True)}">{t("kit_apply_link")}</a>')
    for key, label in (("requirements", "kit_requirements"), ("gaps", "kit_gaps"), ("talking_points", "kit_points")):
        if kit.get(key):
            lines.append(f"\n{t(label)}\n{_items(kit[key])}")
    messages = ["\n".join(lines)[:4000]]
    if kit.get("cover_letter", "").strip():
        messages.append(f"{t('kit_letter')}\n\n<code>{e(kit['cover_letter'].strip())[:3800]}</code>")
    return messages


def _reply_to(con, bot_id, profile_id, uuid):
    row = con.execute("SELECT message_id FROM messages WHERE bot_id=? AND profile_id=? AND uuid=? "
                      "ORDER BY message_id DESC", (bot_id, profile_id, uuid)).fetchone()
    return {"message_id": row["message_id"], "allow_sending_without_reply": True} if row else None


def send_kits(con) -> int:
    """Prepare and send the current profile's pending kits, with its bot. Returns how many were sent."""
    p = profiles.current()
    sent = 0
    for ad in pending(con)[:MAX_PER_RUN]:
        uuid = ad["uuid"]
        key = (p.id, uuid)
        con.execute("INSERT OR IGNORE INTO kits(profile_id, uuid) VALUES(?, ?)", key)
        if con.execute("SELECT sent_at FROM kits WHERE profile_id=? AND uuid=?", key).fetchone()["sent_at"]:
            send_documents(con, p, ad)  # text kit already sent: only the PDFs are left
            continue
        con.execute("UPDATE kits SET attempts = attempts + 1 WHERE profile_id=? AND uuid=?", key)
        con.commit()
        try:
            bot = profiles.bot_of(p)
            row = con.execute("SELECT content FROM kits WHERE profile_id=? AND uuid=?", key).fetchone()
            kit = json.loads(row["content"]) if row["content"] else triage.ai_json(build_prompt(ad), KIT_SCHEMA)
            con.execute("UPDATE kits SET content=?, error=NULL WHERE profile_id=? AND uuid=?",
                        (json.dumps(kit, ensure_ascii=False), *key))
            con.commit()  # the AI answer is kept: a Telegram failure does not cost another AI call
            reply = _reply_to(con, bot.id, p.id, uuid)
            for i, text in enumerate(format_kit(ad, kit)):
                params = dict(chat_id=telegram.chat_id(bot), text=text, parse_mode="HTML",
                              link_preview_options={"is_disabled": True})
                if reply and i == 0:
                    params["reply_parameters"] = reply
                telegram.api("sendMessage", bot=bot, **params)
            con.execute("UPDATE kits SET sent_at=? WHERE profile_id=? AND uuid=?", (db.now_iso(), *key))
            con.commit()
            sent += 1
            log.info("Application kit sent (%s): %s", p.name or p.id, ad["title"])
            if cvdocs.ready(p):
                send_documents(con, p, ad)
        except Exception as e:
            err = str(e)[:500]
            con.execute("UPDATE kits SET error=? WHERE profile_id=? AND uuid=?", (err, *key))
            con.commit()
            log.error("Application kit failed for %s: %s", ad["title"], err)
            attempts = con.execute("SELECT attempts FROM kits WHERE profile_id=? AND uuid=?", key).fetchone()["attempts"]
            if attempts >= MAX_ATTEMPTS:
                try:
                    telegram.send_text(t("kit_failed", title=ad["title"] or "", err=err[:300]))
                except Exception:
                    pass
    return sent


def send_documents(con, p, ad) -> bool:
    """The adapted CV and the cover letter as PDFs, as a reply to the ad. Failures are retried
    at the next runs (3 tries), then reported once; the text kit is never sent again."""
    key = (p.id, ad["uuid"])
    con.execute("UPDATE kits SET docs_attempts = docs_attempts + 1 WHERE profile_id=? AND uuid=?", key)
    con.commit()
    try:
        row = con.execute("SELECT content FROM kits WHERE profile_id=? AND uuid=?", key).fetchone()
        letter = (json.loads(row["content"]) if row["content"] else {}).get("cover_letter", "")
        bot = profiles.bot_of(p)
        files = cvdocs.make_documents(ad, letter, p)
        reply = _reply_to(con, bot.id, p.id, ad["uuid"])
        for kind in ("cv", "letter"):
            if kind in files:
                telegram.send_document(files[kind], t("doc_" + kind), bot=bot, reply=reply)
        con.execute("UPDATE kits SET docs_at=?, docs_error=NULL WHERE profile_id=? AND uuid=?", (db.now_iso(), *key))
        con.commit()
        log.info("CV and letter PDFs sent (%s): %s", p.name or p.id, ad["title"])
        return True
    except Exception as e:
        err = str(e)[:500]
        con.execute("UPDATE kits SET docs_error=? WHERE profile_id=? AND uuid=?", (err, *key))
        con.commit()
        log.error("CV / letter PDFs failed for %s: %s", ad["title"], err)
        attempts = con.execute("SELECT docs_attempts FROM kits WHERE profile_id=? AND uuid=?", key).fetchone()[0]
        if attempts >= MAX_ATTEMPTS:
            try:
                telegram.send_text(t("docs_failed", title=ad["title"] or "", err=err[:300]))
            except Exception:
                pass
        return False
