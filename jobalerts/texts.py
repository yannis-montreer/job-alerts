"""User-facing texts (Telegram messages), in the language set by LANGUAGE in .env."""
from . import config

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
        "language_set": "✅ Les alertes sont maintenant en français : messages et résumés des offres dès le prochain run.",
    },
}


def lang() -> str:
    return config.LANGUAGE if config.LANGUAGE in TEXTS else "en"


def t(key: str, **kw) -> str:
    return TEXTS[lang()][key].format(**kw)


def language_name() -> str:
    return LANGUAGE_NAMES[lang()]
