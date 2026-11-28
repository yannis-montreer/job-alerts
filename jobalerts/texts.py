"""User-facing texts (Telegram messages), in the language set by LANGUAGE in .env."""
from . import profiles

LANGUAGE_NAMES = {"en": "English", "fr": "French"}

TEXTS = {
    "en": {
        "keep": "Keep",
        "reject": "Reject",
        "norwegian": "🇳🇴 <b>Norwegian required</b>",
        "limited": "ℹ️ {source}: limited preview (no description), open the link",
        "see_ad": "View the ad",
        "digest_title": "📋 <b>Also possible today ({n})</b>, weaker matches, no buttons:",
        "none_today": "No new relevant ads today ({n} ad(s) analysed).",
        "source_failed": "⚠️ {source} source failed (other sources and sorting OK): {err}",
        "login_expiry": "⚠️ The Claude login on this computer expires in {days} day(s). "
                        'Renew it: & "$env:USERPROFILE\\.local\\bin\\claude.exe" auth login',
        "run_failed": "❌ {mode} run failed: {err}\nRetrying at the next scheduled run.",
        "mode_full": "Full",
        "mode_collect": "Catch-up",
        "kit_title": "📝 <b>Application kit</b>: {title} ({employer})",
        "kit_deadline": "⏰ <b>Deadline:</b> {v}",
        "kit_contact": "👤 <b>Contact:</b> {v}",
        "kit_how": "📨 <b>How to apply:</b> {v}",
        "kit_apply_link": "Apply / open the ad",
        "kit_requirements": "<b>Key requirements</b>",
        "kit_gaps": "<b>Gaps to prepare for</b>",
        "kit_points": "<b>Points to highlight</b>",
        "kit_letter": "✉️ <b>Draft cover letter</b> (tap to copy, then adapt):",
        "kit_failed": "⚠️ The application kit for \"{title}\" could not be prepared after 3 tries: {err}",
        "doc_cv": "📄 CV adapted to this job (PDF, in English): open it, check it, then attach it to your application.",
        "doc_letter": "✉️ Cover letter (PDF): check it and complete anything in [brackets] before sending.",
        "docs_failed": "⚠️ The CV and letter PDFs for \"{title}\" could not be made after 3 tries: {err}",
        "boot_restarted": "🔌 The computer restarted ({time}).",
        "boot_unexpected": "⚡ The computer restarted after an unexpected shutdown, e.g. a power cut ({time}).",
        "boot_login": "⚠️ Windows is still not logged in {minutes} minutes after the restart: the dashboard, the Telegram buttons and the daily alerts wait for a login. Log in to the computer.",
        "boot_back": "✅ Back up ({time}): dashboard, Telegram buttons and daily alerts are running.",
        "claude_login_expiry": "⚠️ The Claude login on the computer expires in {days} day(s): then the ads are no longer sorted. Best fix, once a year: create a yearly token on the dashboard's AI page (at the computer): {link}",
        "claude_token_expiry": "⚠️ The yearly Claude token expires in {days} day(s): create a new one on the dashboard's AI page (at the computer): {link}",
        "claude_logged_out": "❌ Claude is not logged in on the computer: the ads are not sorted and no application kit is made. Fix it on the dashboard's AI page (at the computer): {link}",
        "claude_failed": "❌ Claude refused the login during a run: the ads waiting are sorted at the next run once fixed. Fix it on the dashboard's AI page (at the computer): {link}",
        "updates_lost": "⚠️ {n} Telegram click(s) or message(s) never reached this tool: another program may be using the bot token, or they were older than 24 h (computer off). Check your last Keep / Reject clicks.",
        "language_set": "✅ Job alerts are now in English: messages and job summaries from the next run.",
    },
    "fr": {
        "keep": "Garder",
        "reject": "Rejeter",
        "norwegian": "🇳🇴 <b>Norvégien demandé</b>",
        "limited": "ℹ️ {source} : aperçu limité (pas de description), ouvre le lien",
        "see_ad": "Voir l'offre",
        "digest_title": "📋 <b>Aussi possible aujourd'hui ({n})</b>, correspondances plus faibles, sans boutons :",
        "none_today": "Aucune nouvelle offre pertinente aujourd'hui ({n} annonce(s) analysée(s)).",
        "source_failed": "⚠️ Source {source} en échec (autres sources et tri OK) : {err}",
        "login_expiry": "⚠️ Le login Claude de cet ordinateur expire dans {days} jour(s). "
                        'Relancer : & "$env:USERPROFILE\\.local\\bin\\claude.exe" auth login',
        "run_failed": "❌ Run {mode} échoué : {err}\nRéessai au prochain run planifié.",
        "mode_full": "complet",
        "mode_collect": "rattrapage",
        "kit_title": "📝 <b>Kit de candidature</b> : {title} ({employer})",
        "kit_deadline": "⏰ <b>Date limite :</b> {v}",
        "kit_contact": "👤 <b>Contact :</b> {v}",
        "kit_how": "📨 <b>Comment postuler :</b> {v}",
        "kit_apply_link": "Postuler / ouvrir l'offre",
        "kit_requirements": "<b>Exigences clés</b>",
        "kit_gaps": "<b>Points faibles à préparer</b>",
        "kit_points": "<b>Points à mettre en avant</b>",
        "kit_letter": "✉️ <b>Brouillon de lettre de motivation</b> (touche pour copier, puis adapte) :",
        "kit_failed": "⚠️ Le kit de candidature pour « {title} » n'a pas pu être préparé après 3 essais : {err}",
        "doc_cv": "📄 CV adapté à cette offre (PDF, en anglais) : ouvre-le, vérifie-le, puis joins-le à ta candidature.",
        "doc_letter": "✉️ Lettre de motivation (PDF) : vérifie-la et complète ce qui est entre [crochets] avant l'envoi.",
        "docs_failed": "⚠️ Les PDF du CV et de la lettre pour « {title} » n'ont pas pu être faits après 3 essais : {err}",
        "boot_restarted": "🔌 L'ordinateur a redémarré ({time}).",
        "boot_unexpected": "⚡ L'ordinateur a redémarré après un arrêt inattendu, par ex. une coupure de courant ({time}).",
        "boot_login": "⚠️ Windows n'est toujours pas connecté {minutes} minutes après le redémarrage : le tableau de bord, les boutons Telegram et les alertes du jour attendent une connexion. Connecte-toi à l'ordinateur.",
        "boot_back": "✅ De retour ({time}) : tableau de bord, boutons Telegram et alertes du jour fonctionnent.",
        "claude_login_expiry": "⚠️ Le login Claude de l'ordinateur expire dans {days} jour(s) : ensuite les offres ne sont plus triées. Le mieux, une fois par an : crée un token annuel sur la page IA du tableau de bord (à l'ordinateur) : {link}",
        "claude_token_expiry": "⚠️ Le token Claude annuel expire dans {days} jour(s) : crées-en un nouveau sur la page IA du tableau de bord (à l'ordinateur) : {link}",
        "claude_logged_out": "❌ Claude n'est pas connecté sur l'ordinateur : les offres ne sont pas triées et aucun kit de candidature n'est fait. Corrige-le sur la page IA du tableau de bord (à l'ordinateur) : {link}",
        "claude_failed": "❌ Claude a refusé le login pendant un run : les offres en attente seront triées au prochain run une fois corrigé. Corrige-le sur la page IA du tableau de bord (à l'ordinateur) : {link}",
        "updates_lost": "⚠️ {n} clic(s) ou message(s) Telegram ne sont jamais arrivés à cet outil : un autre programme utilise peut-être le token du bot, ou ils avaient plus de 24 h (ordinateur éteint). Vérifie tes derniers clics Garder / Rejeter.",
        "language_set": "✅ Les alertes sont maintenant en français : messages et résumés des offres dès le prochain run.",
    },
}


def lang() -> str:
    language = profiles.current().language
    return language if language in TEXTS else "en"


def t(key: str, **kw) -> str:
    return TEXTS[lang()][key].format(**kw)


def language_name() -> str:
    return LANGUAGE_NAMES[lang()]
