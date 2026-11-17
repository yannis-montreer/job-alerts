"""User-facing texts (Telegram messages), in the language set by LANGUAGE in .env."""
from . import config

LANGUAGE_NAMES = {"en": "English", "fr": "French"}

TEXTS = {
    "en": {
        "keep": "Keep",
        "reject": "Reject",
        "norwegian": "🇳🇴 <b>Norwegian required</b>",
        "finn_limited": "ℹ️ Finn: limited preview (no description), open the link",
        "see_ad": "View the ad",
        "none_today": "No new relevant ads today ({n} ad(s) analysed).",
        "finn_failed": "⚠️ Finn source failed (NAV and sorting OK): {err}",
        "login_expiry": "⚠️ The Claude login on this computer expires in {days} day(s). "
                        'Renew it: & "$env:USERPROFILE\\.local\\bin\\claude.exe" auth login',
        "run_failed": "❌ {mode} run failed: {err}\nRetrying at the next scheduled run.",
        "mode_full": "Full",
        "mode_collect": "Catch-up",
    },
    "fr": {
        "keep": "Garder",
        "reject": "Rejeter",
        "norwegian": "🇳🇴 <b>Norvégien demandé</b>",
        "finn_limited": "ℹ️ Finn : aperçu limité (pas de description), ouvre le lien",
        "see_ad": "Voir l'offre",
        "none_today": "Aucune nouvelle offre pertinente aujourd'hui ({n} annonce(s) analysée(s)).",
        "finn_failed": "⚠️ Source Finn en échec (NAV et tri OK) : {err}",
        "login_expiry": "⚠️ Le login Claude de cet ordinateur expire dans {days} jour(s). "
                        'Relancer : & "$env:USERPROFILE\\.local\\bin\\claude.exe" auth login',
        "run_failed": "❌ Run {mode} échoué : {err}\nRéessai au prochain run planifié.",
        "mode_full": "complet",
        "mode_collect": "rattrapage",
    },
}


def lang() -> str:
    return config.LANGUAGE if config.LANGUAGE in TEXTS else "en"


def t(key: str, **kw) -> str:
    return TEXTS[lang()][key].format(**kw)


def language_name() -> str:
    return LANGUAGE_NAMES[lang()]
