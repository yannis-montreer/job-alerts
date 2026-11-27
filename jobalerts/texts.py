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
