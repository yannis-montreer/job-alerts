"""Telegram without a permanent process.

- send_offers: sends the relevant ads with Keep / Reject buttons.
- collect_updates: reads getUpdates (Telegram keeps updates for 24 h) and stores clicks and
  replies in SQLite. Only the configured chat_id is accepted.

Find your chat_id: write to your bot, then  python -m jobalerts.telegram --whoami
"""
import html
import logging
import os
import sys

import requests

from . import config, db, finn, linkedin
from .texts import t

log = logging.getLogger(__name__)
SOURCE_NAMES = {"finn": "Finn", "linkedin": "LinkedIn"}  # sources that can lack a description
API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 30


def _creds():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return token, chat


def api(method: str, **params):
    token, _ = _creds()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing in .env")
    r = requests.post(API.format(token=token, method=method), json=params, timeout=TIMEOUT)
    try:
        data = r.json()
    except ValueError:
        r.raise_for_status()
        raise
    if not data.get("ok"):
        # Never include the URL (it contains the token) in the error message.
        raise RuntimeError(f"Telegram {method}: {data.get('description')} (HTTP {r.status_code})")
    return data["result"]


def chat_id() -> int:
    _, chat = _creds()
    if not chat:
        raise RuntimeError("TELEGRAM_CHAT_ID missing in .env")
    return int(chat)


# ---------- sending ----------

def keyboard(uuid: str, selected: str | None = None):
    keep = ("✔ " if selected == "keep" else "") + t("keep")
    rej = ("✔ " if selected == "reject" else "") + t("reject")
    return {"inline_keyboard": [[
        {"text": keep, "callback_data": f"k:{uuid}"},
        {"text": rej, "callback_data": f"r:{uuid}"},
    ]]}


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
    lines += ["", e(ad["summary_fr"] or ""), "",
              f'<a href="{e(ad["url"] or "", quote=True)}">{e(t("see_ad"))}</a>']
    return "\n".join(lines)


def pending_ads(con) -> tuple[list, list]:
    """Relevant ads not sent yet: (one message each, digest lines), following the settings."""
    sql = "SELECT * FROM ads WHERE relevant=1 AND sent_at IS NULL AND score >= ?"
    if config.NORWEGIAN_REQUIRED == "reject":
        sql += " AND NOT coalesce(norwegian_required, 0)"
    ads = con.execute(sql + " ORDER BY score DESC, first_seen", (config.DIGEST_MIN_SCORE,)).fetchall()
    full = [a for a in ads if a["score"] >= config.MESSAGE_MIN_SCORE]
    digest = [a for a in ads if a["score"] < config.MESSAGE_MIN_SCORE]
    return full, digest


def format_digest_line(ad) -> str:
    e = html.escape
    flag = " 🇳🇴" if ad["norwegian_required"] else ""
    return (f'• <a href="{e(ad["url"] or "", quote=True)}">{e(ad["title"] or "")}</a>'
            f' · {e(ad["employer"] or "?")} · {e((ad["municipal"] or "?").title())} · {ad["score"]}/5{flag}')


def send_offers(con) -> int:
    """Send the pending ads. Returns how many ads were sent (messages + digest lines)."""
    full, digest = pending_ads(con)
    sent = 0
    for ad in full:
        msg = api(
            "sendMessage", chat_id=chat_id(), text=format_offer(ad), parse_mode="HTML",
            link_preview_options={"is_disabled": True}, reply_markup=keyboard(ad["uuid"]),
        )
        con.execute("INSERT OR REPLACE INTO sent_messages(message_id, uuid, sent_at) VALUES(?,?,?)",
                    (msg["message_id"], ad["uuid"], db.now_iso()))
        con.execute("UPDATE ads SET sent_at=? WHERE uuid=?", (db.now_iso(), ad["uuid"]))
        con.commit()  # commit after each send: never send twice
        sent += 1
    # The digest: one short list, split only if it exceeds Telegram's 4096-character limit.
    chunk, chunk_ads = [t("digest_title", n=len(digest))], []
    for i, ad in enumerate(digest):
        line = format_digest_line(ad)
        if len("\n".join(chunk + [line])) > 3900:
            sent += _send_digest(con, chunk, chunk_ads)
            chunk, chunk_ads = [], []
        chunk.append(line)
        chunk_ads.append(ad)
    if chunk_ads:
        sent += _send_digest(con, chunk, chunk_ads)
    log.info("Telegram: %d message(s), %d in the digest", len(full), len(digest))
    return sent


def _send_digest(con, lines, ads) -> int:
    api("sendMessage", chat_id=chat_id(), text="\n".join(lines), parse_mode="HTML",
        link_preview_options={"is_disabled": True})
    now = db.now_iso()
    con.executemany("UPDATE ads SET sent_at=? WHERE uuid=?", [(now, a["uuid"]) for a in ads])
    con.commit()
    return len(ads)


def send_text(text: str):
    api("sendMessage", chat_id=chat_id(), text=text, link_preview_options={"is_disabled": True})


# ---------- reading clicks and replies ----------

def _is_owner(update: dict) -> bool:
    owner = chat_id()
    if "callback_query" in update:
        cq = update["callback_query"]
        msg_chat = (cq.get("message") or {}).get("chat", {}).get("id")
        return cq.get("from", {}).get("id") == owner and msg_chat == owner
    msg = update.get("message") or {}
    return msg.get("chat", {}).get("id") == owner and msg.get("from", {}).get("id") == owner


def _apply_notes(con):
    con.execute(
        "UPDATE decisions SET reason=(SELECT group_concat(text, ' / ') FROM notes WHERE notes.uuid=decisions.uuid) "
        "WHERE EXISTS (SELECT 1 FROM notes WHERE notes.uuid=decisions.uuid)"
    )
    con.commit()


def collect_updates(con) -> dict:
    """Read updates since the last offset and store decisions and notes."""
    stats = dict(updates=0, decisions=0, notes=0, ignored=0)
    offset = int(db.get_meta(con, "tg_offset", "0"))
    to_ack = []  # (chat_id, message_id, uuid, decision)
    while True:
        # allowed_updates is remembered by Telegram: always include callback_query,
        # otherwise button clicks are dropped.
        updates = api("getUpdates", offset=offset or None, timeout=0, limit=100,
                      allowed_updates=["message", "callback_query"])
        if not updates:
            break
        # Telegram numbers updates one by one: a jump means someone else read them first.
        if offset and updates[0]["update_id"] > offset:
            lost = updates[0]["update_id"] - offset
            stats["lost"] = stats.get("lost", 0) + lost
            log.warning("%d Telegram update(s) missing before %s: read by another program, or expired",
                        lost, updates[0]["update_id"])
        for u in updates:
            stats["updates"] += 1
            offset = max(offset, u["update_id"] + 1)
            if not _is_owner(u):
                stats["ignored"] += 1
                log.warning("Update %s ignored: sender not allowed", u["update_id"])
                continue
            if "callback_query" in u:
                cq = u["callback_query"]
                kind, _, uuid = (cq.get("data") or "").partition(":")
                if kind in ("k", "r") and uuid:
                    decision = "keep" if kind == "k" else "reject"
                    cur = con.execute(
                        "INSERT OR IGNORE INTO decisions(uuid, decision, decided_at, telegram_update_id) VALUES(?,?,?,?)",
                        (uuid, decision, db.now_iso(), u["update_id"]))
                    stats["decisions"] += cur.rowcount
                    to_ack.append((cq["message"]["chat"]["id"], cq["message"]["message_id"], uuid, decision))
            else:
                msg = u.get("message") or {}
                text = (msg.get("text") or "").strip()
                reply_to = (msg.get("reply_to_message") or {}).get("message_id")
                row = con.execute("SELECT uuid FROM sent_messages WHERE message_id=?", (reply_to,)).fetchone() if reply_to else None
                if text and row:
                    cur = con.execute("INSERT OR IGNORE INTO notes(uuid, text, telegram_update_id) VALUES(?,?,?)",
                                      (row["uuid"], text, u["update_id"]))
                    stats["notes"] += cur.rowcount
                else:
                    stats["ignored"] += 1
        con.commit()
        db.set_meta(con, "tg_offset", str(offset))  # only after the DB commit: nothing lost
    _apply_notes(con)
    if stats.get("lost"):
        try:
            send_text(t("updates_lost", n=stats["lost"]))
        except Exception as e:
            log.error("Could not send the lost-updates warning: %s", e)
    # Show the choice on the buttons (last choice per message).
    last = {}
    for chat, mid, uuid, decision in to_ack:
        last[(chat, mid)] = (uuid, decision)
    for (chat, mid), (uuid, decision) in last.items():
        try:
            api("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup=keyboard(uuid, decision))
        except RuntimeError as e:  # e.g. "message is not modified": harmless
            log.info("Button update skipped: %s", e)
    log.info("Telegram: %s", stats)
    return stats


# ---------- setup help: find the chat_id ----------

def whoami():
    """Show who wrote to the bot (without consuming the updates)."""
    me = api("getMe")
    print(f"Bot: @{me['username']}")
    seen = {}
    for u in api("getUpdates", timeout=0, allowed_updates=["message", "callback_query"]):
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
