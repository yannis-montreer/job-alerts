"""Telegram, per bot and per profile, without a permanent process (the listener is optional).

- send_offers: sends the current profile's relevant ads with its bot, with Keep / Reject buttons.
- collect_updates: reads a bot's updates (getUpdates) and stores clicks and replies for the
  profile each message belongs to. Only the bot's own chat id is accepted.

Button data is "k:<profile>:<ad>" (Keep) or "r:<profile>:<ad>" (Reject); buttons sent before
profiles ("k:<ad>") count for the bot's first profile.

Find your chat_id: write to your bot, then  python -m jobalerts.telegram --whoami
"""
import html
import json
import logging
import os
import sys

import requests

from . import db, finn, linkedin, profiles
from .texts import t

log = logging.getLogger(__name__)
SOURCE_NAMES = {"finn": "Finn", "linkedin": "LinkedIn"}  # sources that can lack a description
API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 30
# An ad as seen by one profile: the ad itself plus that profile's sorting result.
MATCH_SELECT = ("SELECT a.uuid, a.first_seen, a.title, a.employer, a.municipal, a.url, a.application_url, "
                "a.description, a.source, m.profile_id, m.triaged_at, m.relevant, m.score, m.summary, "
                "m.norwegian_required, m.sent_at FROM matches m JOIN ads a ON a.uuid = m.uuid")


def _bot(bot=None):
    return bot or profiles.bot_of(profiles.current())


def api(method: str, _timeout: int = TIMEOUT, bot=None, **params):
    b = _bot(bot)
    token = b.token
    if not token:
        raise RuntimeError(f"Telegram token missing for bot {b.name or b.id} (dashboard, Output)")
    r = requests.post(API.format(token=token, method=method), json=params, timeout=_timeout)
    try:
        data = r.json()
    except ValueError:
        r.raise_for_status()
        raise
    if not data.get("ok"):
        # Never include the URL (it contains the token) in the error message.
        raise RuntimeError(f"Telegram {method}: {data.get('description')} (HTTP {r.status_code})")
    return data["result"]


def chat_id(bot=None) -> int:
    b = _bot(bot)
    if not str(b.chat_id).strip():
        raise RuntimeError(f"Chat id missing for bot {b.name or b.id} (dashboard, Output)")
    return int(b.chat_id)


# ---------- sending ----------

def keyboard(uuid: str, selected: str | None = None, profile_id: str | None = None):
    pid = profile_id or profiles.current().id
    keep = ("✔ " if selected == "keep" else "") + t("keep")
    rej = ("✔ " if selected == "reject" else "") + t("reject")
    return {"inline_keyboard": [[
        {"text": keep, "callback_data": f"k:{pid}:{uuid}"},
        {"text": rej, "callback_data": f"r:{pid}:{uuid}"},
    ]]}


def parse_callback(data: str, bot) -> tuple[str, str, str]:
    """(kind, profile id, ad uuid) from a button's data."""
    kind, _, rest = (data or "").partition(":")
    head, sep, tail = rest.partition(":")
    if sep and profiles.ID_RE.fullmatch(head) and profiles.get(head):
        return kind, head, tail
    owners = profiles.profiles_of(bot)  # buttons from before profiles
    return kind, (owners[0].id if owners else profiles.DEFAULT_PROFILE_ID), rest


def format_offer(ad) -> str:
    e = html.escape
    lines = [
        f"<b>{e(ad['title'] or '')}</b>",
        f"{e(ad['employer'] or '?')} · {e((ad['municipal'] or '?').title())} · {ad['score']}/5",
    ]
    if ad["norwegian_required"]:
        lines.append(t("norwegian"))
    if ad["source"] in SOURCE_NAMES and ad["description"] in (None, "", finn.LIMITED_INFO, linkedin.LIMITED_INFO):
        lines.append(t("limited", source=SOURCE_NAMES[ad["source"]]))
    lines += ["", e(ad["summary"] or ""), "",
              f'<a href="{e(ad["url"] or "", quote=True)}">{e(t("see_ad"))}</a>']
    return "\n".join(lines)


def pending_ads(con) -> tuple[list, list]:
    """The current profile's relevant ads not sent yet: (one message each, digest lines)."""
    p = profiles.current()
    sql = MATCH_SELECT + " WHERE m.profile_id=? AND m.relevant=1 AND m.sent_at IS NULL AND m.score >= ?"
    if p.norwegian == "reject":
        sql += " AND NOT coalesce(m.norwegian_required, 0)"
    ads = con.execute(sql + " ORDER BY m.score DESC, a.first_seen", (p.id, p.digest_min)).fetchall()
    full = [a for a in ads if a["score"] >= p.message_min]
    digest = [a for a in ads if a["score"] < p.message_min]
    return full, digest


def format_digest_line(ad) -> str:
    e = html.escape
    flag = " 🇳🇴" if ad["norwegian_required"] else ""
    return (f'• <a href="{e(ad["url"] or "", quote=True)}">{e(ad["title"] or "")}</a>'
            f' · {e(ad["employer"] or "?")} · {e((ad["municipal"] or "?").title())} · {ad["score"]}/5{flag}')


def _mark_sent(con, p, uuids):
    now = db.now_iso()
    con.executemany("UPDATE matches SET sent_at=? WHERE profile_id=? AND uuid=?", [(now, p.id, u) for u in uuids])
    con.commit()


def send_offers(con) -> int:
    """Send the current profile's pending ads with its bot. Returns how many were sent."""
    p = profiles.current()
    bot = profiles.bot_of(p)
    full, digest = pending_ads(con)
    sent = 0
    for ad in full:
        msg = api(
            "sendMessage", bot=bot, chat_id=chat_id(bot), text=format_offer(ad), parse_mode="HTML",
            link_preview_options={"is_disabled": True}, reply_markup=keyboard(ad["uuid"], None, p.id),
        )
        con.execute("INSERT OR REPLACE INTO messages(bot_id, message_id, profile_id, uuid, sent_at) VALUES(?,?,?,?,?)",
                    (bot.id, msg["message_id"], p.id, ad["uuid"], db.now_iso()))
        _mark_sent(con, p, [ad["uuid"]])  # commit after each send: never send twice
        sent += 1
    # The digest: one short list, split only if it exceeds Telegram's 4096-character limit.
    chunk, chunk_ads = [t("digest_title", n=len(digest))], []
    for ad in digest:
        line = format_digest_line(ad)
        if len("\n".join(chunk + [line])) > 3900:
            sent += _send_digest(con, chunk, chunk_ads)
            chunk, chunk_ads = [], []
        chunk.append(line)
        chunk_ads.append(ad)
    if chunk_ads:
        sent += _send_digest(con, chunk, chunk_ads)
    log.info("Telegram (%s): %d message(s), %d in the digest", p.name or p.id, len(full), len(digest))
    return sent


def _send_digest(con, lines, ads) -> int:
    p = profiles.current()
    bot = profiles.bot_of(p)
    api("sendMessage", bot=bot, chat_id=chat_id(bot), text="\n".join(lines), parse_mode="HTML",
        link_preview_options={"is_disabled": True})
    _mark_sent(con, p, [a["uuid"] for a in ads])
    return len(ads)


def send_document(path, caption: str = "", bot=None, reply: dict | None = None):
    """Send a file (e.g. a PDF): the user can open it and save it from Telegram."""
    b = _bot(bot)
    if not b.token:
        raise RuntimeError(f"Telegram token missing for bot {b.name or b.id} (dashboard, Output)")
    data = {"chat_id": str(chat_id(b)), "caption": caption}
    if reply:
        data["reply_parameters"] = json.dumps(reply)
    with open(path, "rb") as fh:
        r = requests.post(API.format(token=b.token, method="sendDocument"), data=data,
                          files={"document": (os.path.basename(path), fh, "application/pdf")}, timeout=120)
    result = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not result.get("ok"):
        raise RuntimeError(f"Telegram sendDocument: {result.get('description') or r.status_code}")
    return result["result"]


def send_text(text: str, bot=None):
    b = _bot(bot)
    api("sendMessage", bot=b, chat_id=chat_id(b), text=text, link_preview_options={"is_disabled": True})


# ---------- reading clicks and replies ----------

def _is_owner(update: dict, bot) -> bool:
    owner = chat_id(bot)
    if "callback_query" in update:
        cq = update["callback_query"]
        msg_chat = (cq.get("message") or {}).get("chat", {}).get("id")
        return cq.get("from", {}).get("id") == owner and msg_chat == owner
    msg = update.get("message") or {}
    return msg.get("chat", {}).get("id") == owner and msg.get("from", {}).get("id") == owner


def _apply_notes(con):
    same = "notes.uuid=decisions.uuid AND notes.profile_id=decisions.profile_id"
    con.execute(
        f"UPDATE decisions SET reason=(SELECT group_concat(text, ' / ') FROM notes WHERE {same}) "
        f"WHERE EXISTS (SELECT 1 FROM notes WHERE {same})"
    )
    con.commit()


def _answer(cq: dict, bot, text: str = ""):
    """Acknowledge a button tap at once: Telegram drops unanswered taps after a few minutes."""
    try:
        api("answerCallbackQuery", bot=bot, callback_query_id=cq["id"], text=text)
    except Exception as e:  # too old, or already answered: harmless
        log.info("Tap not acknowledged: %s", e)


def _profile_or_first(profile_id: str, bot):
    return profiles.get(profile_id) or (profiles.profiles_of(bot) or profiles.all_profiles())[0]


def collect_updates(con, wait: int = 0, bot=None) -> dict:
    """Read a bot's updates since its last offset and store decisions and notes.

    bot: default, the current profile's bot. wait > 0: long polling (the always-on listener):
    the first request waits up to `wait` seconds for something to arrive."""
    bot = _bot(bot)
    long_poll = wait > 0
    stats = dict(updates=0, decisions=0, notes=0, ignored=0)
    offset_key = f"tg_offset:{bot.id}"
    offset = int(db.get_meta(con, offset_key, "0"))
    to_ack = []  # (chat_id, message_id, uuid, decision, profile)
    while True:
        # allowed_updates is remembered by Telegram: always include callback_query,
        # otherwise button clicks are dropped.
        updates = api("getUpdates", bot=bot, _timeout=wait + 20, offset=offset or None, timeout=wait, limit=100,
                      allowed_updates=["message", "callback_query"])
        wait = 0
        if not updates:
            break
        # Telegram numbers updates one by one: a jump means someone else read them first.
        if offset and updates[0]["update_id"] > offset:
            lost = updates[0]["update_id"] - offset
            stats["lost"] = stats.get("lost", 0) + lost
            log.warning("Bot %s: %d Telegram update(s) missing before %s: read by another program, or expired",
                        bot.id, lost, updates[0]["update_id"])
        for u in updates:
            stats["updates"] += 1
            offset = max(offset, u["update_id"] + 1)
            if not _is_owner(u, bot):
                if "callback_query" in u:
                    _answer(u["callback_query"], bot)
                stats["ignored"] += 1
                log.warning("Update %s ignored: sender not allowed", u["update_id"])
                continue
            if "callback_query" in u:
                cq = u["callback_query"]
                kind, pid, uuid = parse_callback(cq.get("data"), bot)
                profile = _profile_or_first(pid, bot)
                with profiles.use(profile):
                    if kind in ("k", "r") and uuid:
                        decision = "keep" if kind == "k" else "reject"
                        cur = con.execute(
                            "INSERT OR IGNORE INTO decisions(profile_id, uuid, decision, decided_at, bot_id, "
                            "telegram_update_id) VALUES(?,?,?,?,?,?)",
                            (profile.id, uuid, decision, db.now_iso(), bot.id, u["update_id"]))
                        stats["decisions"] += cur.rowcount
                        to_ack.append((cq["message"]["chat"]["id"], cq["message"]["message_id"], uuid, decision, profile))
                        _answer(cq, bot, "✔ " + t("keep" if decision == "keep" else "reject"))
                    else:
                        _answer(cq, bot, "✔")
            else:
                msg = u.get("message") or {}
                text = (msg.get("text") or "").strip()
                reply_to = (msg.get("reply_to_message") or {}).get("message_id")
                row = con.execute("SELECT profile_id, uuid FROM messages WHERE bot_id=? AND message_id=?",
                                  (bot.id, reply_to)).fetchone() if reply_to else None
                if text and row:
                    cur = con.execute(
                        "INSERT OR IGNORE INTO notes(profile_id, uuid, text, bot_id, telegram_update_id) VALUES(?,?,?,?,?)",
                        (row["profile_id"], row["uuid"], text, bot.id, u["update_id"]))
                    stats["notes"] += cur.rowcount
                else:
                    stats["ignored"] += 1
        con.commit()
        db.set_meta(con, offset_key, str(offset))  # only after the DB commit: nothing lost
    _apply_notes(con)
    if stats.get("lost"):
        try:
            with profiles.use(_profile_or_first("", bot)):
                send_text(t("updates_lost", n=stats["lost"]), bot)
        except Exception as e:
            log.error("Could not send the lost-updates warning: %s", e)
    # Show the choice on the buttons (last choice per message).
    last = {}
    for chat, mid, uuid, decision, profile in to_ack:
        last[(chat, mid)] = (uuid, decision, profile)
    for (chat, mid), (uuid, decision, profile) in last.items():
        try:
            with profiles.use(profile):
                api("editMessageReplyMarkup", bot=bot, chat_id=chat, message_id=mid,
                    reply_markup=keyboard(uuid, decision, profile.id))
        except RuntimeError as e:  # e.g. "message is not modified": harmless
            log.info("Button update skipped: %s", e)
    (log.info if stats["updates"] or not long_poll else log.debug)("Telegram (bot %s): %s", bot.id, stats)
    return stats


# ---------- setup help: find the chat_id ----------

def whoami(bot=None):
    """Show who wrote to the bot (without consuming the updates)."""
    bot = _bot(bot)
    me = api("getMe", bot=bot)
    print(f"Bot: @{me['username']}")
    seen = {}
    for u in api("getUpdates", bot=bot, timeout=0, allowed_updates=["message", "callback_query"]):
        m = u.get("message") or {}
        if m.get("chat"):
            seen[m["chat"]["id"]] = (m["chat"].get("type"), m["from"].get("first_name"), m["from"].get("username"))
    if not seen:
        print("No message received: first write something to the bot in Telegram, then run this again.")
    for cid, (typ, name, user) in seen.items():
        print(f"chat_id={cid} type={typ} first_name={name} username={user}")


if __name__ == "__main__":
    from . import config  # noqa: F401  (loads .env)
    if "--whoami" in sys.argv:
        whoami()
