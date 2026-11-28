# job-alerts

A small Windows tool that watches job ads in Norway every morning, sorts them against your
own profile with Claude or Gemini, and sends the relevant ones to you on Telegram with **Keep** /
**Reject** buttons. Your clicks and replies teach the sorting your preferences over time.

- **Sources:** the public NAV / Arbeidsplassen feed (full ad text), and optionally Finn.no and
  LinkedIn through your own alert emails (title, employer and location only; for Finn, the
  full ad if Finn gave you written permission, see below). Each source can be switched off.
- **Preferences:** target roles ("main" or "also OK") and things to always reject, edited in
  the dashboard, on top of a free-text profile.
- **Sorting:** one AI call per run, no keyword filter: the AI reads each ad and your profile.
  Either **Claude** (`claude -p` with your Claude Pro or Max subscription, no API key) or
  **Gemini** (Gemini API with a free API key from Google AI Studio).
- **Delivery:** a Telegram bot. No server or permanent process: the script runs from the
  Windows Task Scheduler.

## Several profiles

One installation can serve several people (or several searches), each with its own **profile**:
profile text, preferences, area, Telegram settings, Gmail labels for the Finn and LinkedIn alerts,
and its own **Telegram bot** (two profiles may share a bot). Pick the profile at the top of the
dashboard's menu; **+ New profile** adds one, and the Output page creates its bot (**+ New bot**,
then token and chat as in step 4).

- Sources are fetched once a day for everyone; each ad goes to the profiles whose area it is in
  (and, for Finn and LinkedIn, whose Gmail label it came from). An ad that suits two profiles is
  sent to both.
- Several profiles can share one inbox for Finn: give each Finn alert its own name, then enter it
  in the profile's **Subject of this profile's Finn alert emails** (Sources page), e.g.
  `Nye annonser: Job alert Anna`. A profile then reads only the alert emails whose subject
  contains that text (case does not matter); empty means every Finn alert.
- Each profile is sorted with its own AI call, so the AI usage grows with the number of profiles.
- Clicks, replies and application kits belong to the profile whose message they answer: one
  person's choices never teach another person's sorting.
- A profile that is not set up yet (no bot, no profile text) is skipped until it is.

Profiles are stored in `profiles.json` and `profiles/<id>/` (git-ignored); bot tokens in `.env`
(`TELEGRAM_BOT_TOKEN` for the first bot, `BOT_B2_TOKEN`... for the others). A setup from before
profiles becomes profile `p1` with bot `b1` the first time a profile setting is saved.

## How a run works

`python -m jobalerts.run` (every morning, 07:30 by default):
1. Reads your Telegram clicks and replies since the last run and stores them (SQLite).
2. Fetches NAV ads published or changed in the last 24 h in your area (longer after missed runs, 7 days max).
3. Reads new Finn alert emails, if configured.
4. Sorts all new ads in one AI call: relevant yes/no, score 1-5, 2-line summary,
   "Norwegian required" flag. Your past decisions are given as examples.
5. Sends the relevant ads to Telegram, or a short "nothing new today" message:
   - strong matches (score 4-5 by default) as one message each, with Keep / Reject buttons;
   - weaker matches (score 3 by default) in **one short daily list**, one line per ad;
   - ads that require Norwegian are flagged, or dropped with `NORWEGIAN_REQUIRED=reject`.
   Set `MESSAGE_MIN_SCORE`, `DIGEST_MIN_SCORE` and `NORWEGIAN_REQUIRED` in `.env` or in the
   dashboard to get fewer or more ads.

`python -m jobalerts.run --collect-only` only reads your clicks. It runs every 10 minutes
in the morning (07:40-10:30) so the ✔ appears on the button you chose. A click outside that
window is not lost: Telegram keeps it 24 h and the next morning run stores it.

**Tap acknowledgement.** Telegram drops a button tap after about two minutes if the bot does not
answer it, so a check every 10 minutes loses taps. The **always-on dashboard** (see *Phone access*
below) therefore also listens to Telegram all day: each tap is saved and answered within seconds,
the ✔ appears at once, and the scheduled click collection then leaves Telegram to it. Without the
always-on dashboard, only written replies are reliable; set it up even if you don't use a phone.

When you tap **Keep**, the next click collection (within 10 minutes in the morning window, or the
next morning) sends an **application kit** as a reply to the ad: deadline, contact, how to apply,
key requirements, gaps to prepare for, points to highlight, and a draft cover letter to copy and
adapt. It is one extra AI call per kept ad, written only from the ad and your profile.

**CV and cover letter as PDFs.** Upload the CV of each profile in the dashboard (*Profile & area →
CV*, PDF or Word). The tool reads its facts once and, for a PDF, has the AI copy its layout (colours,
columns, photo) into a template; check it with *Preview PDF*. After each Keep, it also sends a CV
adapted to the job (in English, using only facts from the uploaded CV) and the cover letter, both as
PDFs in that layout, ready to attach to the application; they can also be downloaded from History.
PDFs are made on this computer with Microsoft Edge (headless, scripts off). The CV, its photo and the
ad are sent to your AI provider to do this.

If Telegram clicks or messages ever go missing (another program using the same bot token, or
clicks older than 24 h while the computer was off), the tool notices the gap in Telegram's
numbering and warns you on Telegram.

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

- **🌐 button (top left):** dashboard language and Telegram language, English or French.
  Changing the Telegram language asks you to confirm, then sends a confirmation message to
  Telegram; messages and job summaries switch from the next run.
- **Preferences:** target roles (main or also OK) and exclusions, with a filter box, plus
  what the bot has learned from your clicks and replies.
- **Profile & area:** name, what reaches Telegram, area (municipality checkboxes)
  and your free-text profile.
- **Sources:** switch NAV, Finn and LinkedIn on or off; Gmail connection; check buttons.
- **Output (Telegram):** save the bot token from @BotFather, detect your chat id, test message.
- **AI:** choose Claude or Gemini. Claude: install and log in, see when the login expires.
  Gemini: save the API key and test it.
- **Finn alerts:** Gmail address and app password, connection test; optional full-ad reading
  (only with Finn's written permission) and deletion of the stored descriptions.
- **Schedule:** create, update or remove the scheduled tasks.
- **Run now** and **History:** run by hand, see the log, see the ads sent and your decisions.
- **Phone access:** use the dashboard from your phone through Tailscale (see below).

The dashboard only listens on this computer (`127.0.0.1`) and stops when you close its window.
Secrets you type are saved to `.env` and never shown again. Steps 3 to 8 below describe the
same setup by hand.

### Optional: the dashboard on your phone (Tailscale)
If [Tailscale](https://tailscale.com/) runs on this computer and on your phone, the dashboard's
**Phone access** page sets it up in three clicks:
1. **Create and start it** (also needed for reliable Telegram taps): a scheduled task (`<Prefix>-Dashboard`, created by
   `setup_dashboard_task.ps1`) runs the dashboard in the background from logon, on
   `127.0.0.1:8787` (`DASHBOARD_PORT`).
2. **Tailscale address and password:** your computer's Tailscale name (filled in for you) and a
   password of at least 12 characters, stored as a hash. The phone asks for it once, then
   remembers it for 30 days. Changing it logs every phone out.
3. **Turn on Tailscale Serve:** runs `tailscale serve --bg --https=8787 http://127.0.0.1:8787`,
   which makes `https://<your-pc>.<tailnet>.ts.net:8787/` reachable from your own Tailscale
   devices only.
4. **Optional, public access (no Tailscale app on the phone):** `tailscale funnel --bg --https=10000 8787`
   puts the same dashboard on `https://<your-pc>.<tailnet>.ts.net:10000/` for anyone on the
   internet (Funnel allows only ports 443, 8443 and 10000). The login page is then public and the
   password is the only lock, so use a long, unique one. Each remote login and each lock-out
   (5 wrong passwords in 10 minutes) is reported on Telegram; the always-on dashboard is served by
   waitress. Turn it off with `tailscale funnel --https=10000 off`.

**Users.** From the phone, the dashboard password belongs to the **admin** (username `admin`);
on the computer itself the dashboard is always the admin's. The **Users** page creates accounts
(username and a starting password of 12+ characters, kept in `users.json`, git-ignored) and gives
each one alerts (profiles). A user sees only those profiles and changes only what concerns them:
preferences, profile text, CV, area, scores, Telegram language, message time, a full run of their
own profile, their history, and their password (*My account*). A password set by the admin (new account or
reset) must be replaced by the user at their next login before anything else opens. They can add a Telegram bot to a
profile that has none; once its token and chat are saved, only the admin can change or remove it.
Sources, Gmail, the AI, the schedule of the background tasks, other profiles and the run log stay
the admin's. Every login, the admin's or a user's, is reported on the admin's Telegram.

Then open that address on the phone, log in, and add it to the home screen (Chrome: menu ⋮ →
*Add to Home screen* or *Install app*; Safari: Share → *Add to Home Screen*). It opens full
screen like an app. After updating the code, restart the task:
`Stop-ScheduledTask JobAlerts-Dashboard; Start-ScheduledTask JobAlerts-Dashboard`.

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
- In `.env`: `CANDIDATE_NAME` (your first name), `LANGUAGE` (Telegram messages and summaries,
  `en` or `fr`), `DASHBOARD_LANGUAGE` (`en` or `fr`), and optionally
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
   Optionally, filter the alerts into a Gmail label and set `GMAIL_FINN_LABEL`, or give each
   profile the subject of its own alert emails (see Profiles above).

The script reads only emails from `agent@finn.no`, in read-only mode. By default it **does not
download any finn.no page**: Finn's terms and robots.txt forbid automated access without written
permission. Finn ads then arrive with a "limited preview" note; open the link to read them.

**Reading the full Finn ads (only with your own written permission).** Finn may grant a
personal exception if you ask its customer service first. Explain your use: personal,
non-commercial job search; only the ad pages linked from your own alert emails; once a day;
a few seconds between requests; a User-Agent with your contact email. A permission given to
someone else does not cover you. Once you have it in writing:
- set `FINN_FETCH_PAGES=true` and `FINN_CONTACT_EMAIL=you@example.com` in `.env` (or tick the
  box in the dashboard's Finn section);
- each new ad page is then downloaded once, in the morning run, at least 5 seconds apart
  (30 pages per run at most), and only its description is stored locally for sorting;
- never share or republish that content, and delete it when your job search ends:
  `.venv\Scripts\python.exe -m jobalerts.finn --purge` (or the button in the dashboard).
- Follow any further limits Finn sets, and stop if they withdraw the permission.

### 6b. Optional: LinkedIn alerts through Gmail
1. On LinkedIn, search jobs (keywords and location) and turn on a **job alert** with daily emails,
   sent to the same Gmail address.
2. Set `SOURCE_LINKEDIN=true` (or tick the box in the dashboard's Sources page). Optionally,
   filter the alerts into a Gmail label and set `GMAIL_LINKEDIN_LABEL`.
3. When the first alert has arrived, use **Check LinkedIn alerts** in the dashboard: it shows how
   many jobs it could read.

Only emails from `jobalerts-noreply@linkedin.com` are read. **No linkedin.com page is ever
downloaded** (LinkedIn's terms forbid scraping): LinkedIn ads arrive with a "limited preview" note.

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

With several profiles, set each profile's message time on the dashboard's **Schedule** page: it
replaces the morning task with one task per profile (`JobAlerts-p1`, `JobAlerts-p2`...), each
running `python -m jobalerts.run --profile <id>` at that profile's time. Runs that start at the
same minute wait for each other, so new ads are fetched once.

## Files

| Path | What |
| --- | --- |
| `jobalerts/run.py` | One run: clicks, sources, sorting, sending, error reporting |
| `jobalerts/nav.py` | NAV feed (test alone: `python -m jobalerts.nav`) |
| `jobalerts/finn.py` | Finn alert emails, optional full-ad reading |
| `jobalerts/linkedin.py` | LinkedIn alert emails |
| `jobalerts/mail.py` | Shared Gmail reading (read-only) and area filter for these emails |
| `jobalerts/preferences.py` | Target roles and exclusions (`preferences.json`, git-ignored) |
| `jobalerts/profiles.py` | Profiles and bots (`profiles.json`, git-ignored) |
| `jobalerts/triage.py` | Sorting with Claude or Gemini (test alone: `python -m jobalerts.triage`) |
| `jobalerts/telegram.py` | Sending ads, reading clicks and replies |
| `jobalerts/texts.py` | Telegram texts in English and French |
| `jobalerts/config.py` | Paths, area, settings read from `.env` |
| `jobalerts/dashboard.py`, `dashboard.html`, `dashboard_fr.json` | Local setup and status dashboard, French texts |
| `dashboard.cmd` | Double-click to open the dashboard |
| `setup_tasks.ps1` | Creates the scheduled tasks |
| `setup_dashboard_task.ps1` | Always-on dashboard task, for phone access |
| `schedule_task.ps1` | One daily task (used by the Schedule page: one per profile, backup click check) |
| `jobalerts/kit.py` | Application kit sent after a Keep click |
| `jobalerts/cvdocs.py`, `templates/` | CV upload, layout copy, adapted CV and letter PDFs |
| `jobalerts/listener.py` | Always-on Telegram listener (runs in the always-on dashboard) |
| `jobalerts/static/` | App icons for the phone |
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
