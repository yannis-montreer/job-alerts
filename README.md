# job-alerts

A small Windows tool that watches job ads in Norway every morning, sorts them against your
own profile with Claude or Gemini, and sends the relevant ones to you on Telegram with **Keep** /
**Reject** buttons. Your clicks and replies teach the sorting your preferences over time.

- **Sources:** the public NAV / Arbeidsplassen feed (full ad text), and optionally Finn.no
  through your own Finn alert emails (title, employer and location only, see below).
- **Sorting:** one AI call per run, no keyword filter: the AI reads each ad and your profile.
  Either **Claude** (`claude -p` with your Claude Pro or Max subscription, no API key) or
  **Gemini** (Gemini API with a free API key from Google AI Studio).
- **Delivery:** a Telegram bot. No server or permanent process: the script runs from the
  Windows Task Scheduler.

## How a run works

`python -m jobalerts.run` (every morning, 07:30 by default):
1. Reads your Telegram clicks and replies since the last run and stores them (SQLite).
2. Fetches NAV ads published or changed in the last 24 h in your area (longer after missed runs, 7 days max).
3. Reads new Finn alert emails, if configured.
4. Sorts all new ads in one AI call: relevant yes/no, score 1-5, 2-line summary,
   "Norwegian required" flag. Your past decisions are given as examples.
5. Sends the relevant ads to Telegram, or a short "nothing new today" message.

`python -m jobalerts.run --collect-only` only reads your clicks. It runs every 10 minutes
in the morning (07:40-10:30) so the ✔ appears on the button you chose. A click outside that
window is not lost: Telegram keeps it 24 h and the next morning run stores it.

To give a reason, **reply** to an ad message in Telegram (e.g. "too junior", "call center,
not interested"). Replies are used as examples even without a click.

On failure, the error is logged and sent to you on Telegram. Nothing is lost: the next
run picks up whatever was not sorted or sent.

## Requirements

- Windows 10 or 11, on a computer that is on and logged in each morning.
- Python 3.11 or newer.
- For the sorting, one of:
  - a Claude Pro or Max subscription, or
  - a Google account, to create a free Gemini API key. A Gemini app subscription alone does
    not include an API key.
- A Telegram account.
- Optional, for Finn: a Gmail address that receives your Finn alerts.

## Setup

### 1. Get the code and install Python packages
```powershell
git clone https://github.com/yannis-montreer/job-alerts.git
cd job-alerts
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. Open the dashboard (easiest)
Double-click **`dashboard.cmd`** in the project folder (or run
`.venv\Scripts\python.exe -m jobalerts.dashboard`). It opens a local page in your browser
that walks you through every step below, with a test button for each part:

- **You & search:** name, language, area (municipality checkboxes) and your profile.
- **Telegram:** save the bot token, detect your chat id, send a test message.
- **AI:** choose Claude or Gemini. Claude: install and log in, see when the login expires.
  Gemini: save the API key and test it.
- **Finn alerts:** Gmail address and app password, connection test.
- **Schedule:** create, update or remove the scheduled tasks.
- **Run now** and **History:** run by hand, see the log, see the ads sent and your decisions.

The dashboard only listens on this computer (`127.0.0.1`) and stops when you close its window.
Secrets you type are saved to `.env` and never shown again. Steps 3 to 8 below describe the
same setup by hand.

### 3. Set up the AI: Claude or Gemini

**Option A, Gemini (API key).** Open [Google AI Studio](https://aistudio.google.com/apikey),
create an API key, and put it in `.env`:
```
AI_PROVIDER=gemini
GEMINI_API_KEY=your-key
```
`GEMINI_MODEL` is optional (default `gemini-flash-latest`). The free tier is enough for one
call a day. On the free tier, Google's terms allow it to use what is sent (your profile and the
day's ads) to improve its products; the paid tier does not.

**Option B, Claude (subscription, default).** Keep `AI_PROVIDER=claude`. Install the standalone
Claude Code CLI with the official installer, then log in:
```powershell
irm https://claude.ai/install.ps1 | iex
& "$env:USERPROFILE\.local\bin\claude.exe" auth login
```
The script expects `claude.exe` at `%USERPROFILE%\.local\bin\claude.exe`. The login
expires after a while (`claude.exe auth status` shows the date). You get a Telegram warning
5 days before; then run the login command again.

### 4. Create your Telegram bot
1. In Telegram, open **@BotFather**, send `/newbot`, and follow the steps. Copy the token.
2. Copy `.env.example` to `.env` and put the token in `TELEGRAM_BOT_TOKEN`.
3. Send any message to your new bot, then run:
   ```powershell
   .venv\Scripts\python.exe -m jobalerts.telegram --whoami
   ```
   Put the `chat_id` it prints in `TELEGRAM_CHAT_ID`. Only this chat is accepted by the script.

Keep the token private, and don't run another program with the same bot token: it would
read your clicks before the script does.

### 5. Fill in `.env` and your profile
- In `.env`: `CANDIDATE_NAME` (your first name), `LANGUAGE` (`en` or `fr`), and optionally
  your search area (`TARGET_MUNICIPALITIES`, `TARGET_COUNTIES`; default Oslo, Akershus,
  Drammen, Lier, Moss).
- Copy `profile.example.md` to `profile.md` and describe yourself: experience, target roles,
  sectors, languages, what to reject. This is what the AI sorts against, so be concrete.

`.env` and `profile.md` are git-ignored and stay on your computer.

### 6. Optional: Finn alerts through Gmail
1. On finn.no, create a job search alert (Søkeagent) with your area and categories, sent by email
   to your Gmail address.
2. In your Google account, turn on 2-step verification and create an **app password**.
3. Put the address and the app password in `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD`.
   Optionally, filter the alerts into a Gmail label and set `GMAIL_FINN_LABEL`.

The script reads only emails from `agent@finn.no`, in read-only mode. It **does not download
any finn.no page**: Finn's terms and robots.txt forbid automated access without written
permission. Finn ads therefore arrive with a "limited preview" note; open the link to read them.

### 7. Test it by hand
```powershell
.venv\Scripts\python.exe -m jobalerts.run --console
```
The first run sorts the last 24 h of ads and sends the relevant ones to Telegram.

### 8. Schedule it
```powershell
powershell -ExecutionPolicy Bypass -File .\setup_tasks.ps1
```
This creates two tasks for your user: `JobAlerts-Morning` (07:30) and `JobAlerts-CatchUp`
(every 10 min, 07:40-10:30). Change the times with parameters, e.g.
`.\setup_tasks.ps1 -MorningTime 06:45 -CatchUpStart 06:55`. The tasks run only while your
Windows session is open (a locked screen or a disconnected remote session is fine; a reboot
without logging in is not). If the computer was off at 07:30, the run starts as soon as possible.

Run the morning task by hand: `Start-ScheduledTask JobAlerts-Morning`.

## Files

| Path | What |
| --- | --- |
| `jobalerts/run.py` | One run: clicks, sources, sorting, sending, error reporting |
| `jobalerts/nav.py` | NAV feed (test alone: `python -m jobalerts.nav`) |
| `jobalerts/finn.py` | Finn alert emails over IMAP |
| `jobalerts/triage.py` | Sorting with Claude or Gemini (test alone: `python -m jobalerts.triage`) |
| `jobalerts/telegram.py` | Sending ads, reading clicks and replies |
| `jobalerts/texts.py` | Telegram texts in English and French |
| `jobalerts/config.py` | Paths, area, settings read from `.env` |
| `jobalerts/dashboard.py`, `dashboard.html` | Local setup and status dashboard |
| `dashboard.cmd` | Double-click to open the dashboard |
| `setup_tasks.ps1` | Creates the scheduled tasks |
| `tests/` | Unit tests (no network, no secrets) |
| `.github/workflows/ci.yml` | GitHub Actions: runs the tests on each push and pull request |
| `data/jobs.db` | Local database: ads seen, decisions, notes (git-ignored) |
| `logs/jobalerts.log` | Log, 5 rotating files of 1 MB (git-ignored) |

## Development

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
```
The tests cover parsing, filtering, formatting, prompts and the dashboard's `.env` handling.
They never call NAV, Telegram, Gmail, Claude or Gemini. GitHub Actions runs them on Windows for every
push and pull request.

## Notes

- **NAV token:** the script uses NAV's public token, meant for trying the feed. For long-term
  use, ask NAV for a private token at nav.team.arbeidsplassen@nav.no and put it in `NAV_TOKEN`.
- **AI usage:** one call per morning, on your own Claude subscription or Gemini API key.
- **Privacy:** your profile, CV, ads, decisions and logs stay on your computer. The profile and
  the day's ads are sent to the AI you chose for sorting; ads and your clicks go through Telegram.
