"""Always-on Telegram listener, run inside the always-on dashboard (dashboard --service).

Telegram drops a button tap after a couple of minutes if the bot does not acknowledge it, so
the scheduled click collection (every 10 minutes) loses taps. The listener long-polls every
bot all day: each tap is stored and acknowledged within seconds, the button shows the ✔ at
once, and the application kit for a Keep follows right away.

While it runs, it writes a heartbeat per bot in the database; the scheduled runs then leave
that bot to it (two readers at once would make Telegram refuse one of them).
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import claude_auth, db, kit, profiles, telegram

log = logging.getLogger(__name__)
WAIT_S = 50              # long-polling wait per request
HEARTBEAT = "tg_listener_seen"
ACTIVE_FOR = timedelta(seconds=WAIT_S + 70)


def active(con, bot_id: str = profiles.DEFAULT_BOT_ID) -> bool:
    """True if a listener has polled this bot recently."""
    seen = db.get_meta(con, f"{HEARTBEAT}:{bot_id}")
    if not seen:
        return False
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(seen) < ACTIVE_FOR
    except ValueError:
        return False


def _ready(bot) -> bool:
    return bool(bot and bot.token and str(bot.chat_id).strip() and profiles.profiles_of(bot))


class Listener:
    def __init__(self):
        self.stop = threading.Event()
        self.kits_due = threading.Event()
        self.running: set[str] = set()
        self.errors: dict[str, str] = {}

    def start(self):
        threading.Thread(target=self._supervise, name="telegram-bots", daemon=True).start()
        threading.Thread(target=self._kit_loop, name="application-kits", daemon=True).start()
        self.kits_due.set()  # kits left over from before

    def _supervise(self):
        """Start a polling thread for each bot that is set up; bots added later start within a minute."""
        while not self.stop.is_set():
            try:
                for bot in profiles.all_bots():
                    if bot.id not in self.running and _ready(bot):
                        self.running.add(bot.id)
                        threading.Thread(target=self._poll_loop, args=(bot.id,), name=f"telegram-{bot.id}",
                                         daemon=True).start()
            except Exception as e:
                log.error("Telegram listener: %s", e)
            self.stop.wait(60)

    def _poll_loop(self, bot_id: str):
        con = db.connect()
        delay = 5
        try:
            while not self.stop.is_set():
                bot = profiles.get_bot(bot_id)  # read each time: token or chat id may change
                if not _ready(bot):
                    return  # removed or incomplete: the supervisor restarts it when ready
                try:
                    db.set_meta(con, f"{HEARTBEAT}:{bot_id}", db.now_iso())
                    stats = telegram.collect_updates(con, wait=WAIT_S, bot=bot)
                    self.errors.pop(bot_id, None)
                    delay = 5
                    if stats.get("decisions"):
                        self.kits_due.set()
                except Exception as e:
                    self.errors[bot_id] = safe(str(e)[:300])
                    log.error("Telegram listener (bot %s): %s", bot_id, self.errors[bot_id])
                    self.stop.wait(delay)
                    delay = min(delay * 2, 300)
        finally:
            self.running.discard(bot_id)

    def _kit_loop(self):
        con = db.connect()
        checked = 0.0
        while not self.stop.is_set():
            # Kits right after a Keep, and a retry every 30 minutes for failed ones.
            self.kits_due.wait(1800)
            self.kits_due.clear()
            if time.monotonic() - checked > 3 * 3600:  # Claude login / token expiry, for the admin
                checked = time.monotonic()
                try:
                    claude_auth.check_expiry(con)
                except Exception as e:
                    log.warning("Claude login check: %s", str(e)[:200])
            for p in profiles.all_profiles():
                try:
                    with profiles.use(p):
                        kit.send_kits(con)
                except Exception as e:
                    log.error("Application kits (%s): %s", p.id, safe(str(e)[:300]))


def safe(text: str) -> str:
    """Never log a bot token."""
    for bot in profiles.all_bots():
        if bot.token:
            text = text.replace(bot.token, "***")
    return text


def run_forever():  # python -m jobalerts.listener (for testing without the dashboard)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Listener().start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    from . import config  # noqa: F401  (loads .env)
    run_forever()
