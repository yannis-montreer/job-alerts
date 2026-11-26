"""Structured preferences, edited in the dashboard and stored in preferences.json (git-ignored).

They complement profile.md (free text) and take precedence over it where they differ:
- target roles, each "main" (scored normally) or "also" (acceptable, but a weaker match)
- exclusions: kinds of jobs or conditions to always reject
"""
import json

from . import config

PATH = config.ROOT / "preferences.json"
LEVELS = ("main", "also")


def empty() -> dict:
    return {"target_roles": [], "exclude": []}


def load() -> dict:
    if not PATH.exists():
        return empty()
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except ValueError:
        return empty()
    return clean(data)


def clean(data: dict) -> dict:
    """Validate and normalise: trimmed names, known levels, no duplicates, sane lengths."""
    out, seen = empty(), set()
    for r in data.get("target_roles") or []:
        name = str((r or {}).get("name", "")).strip()[:80]
        level = (r or {}).get("level") if (r or {}).get("level") in LEVELS else "main"
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out["target_roles"].append({"name": name, "level": level})
    seen = set()
    for x in data.get("exclude") or []:
        x = str(x).strip()[:120]
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out["exclude"].append(x)
    return out


def save(data: dict) -> dict:
    data = clean(data)
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def prompt_section(data: dict | None = None) -> str:
    """Text added to the sorting prompt, or "" when no preference is set."""
    data = data or load()
    main = [r["name"] for r in data["target_roles"] if r["level"] == "main"]
    also = [r["name"] for r in data["target_roles"] if r["level"] == "also"]
    if not (main or also or data["exclude"]):
        return ""
    lines = ["# PREFERENCES (set in the dashboard: they take precedence over the profile where they differ)"]
    if main:
        lines.append("Main target roles (relevant; score on fit, 4-5 for a good match): " + "; ".join(main))
    if also:
        lines.append("Also acceptable roles (relevant if the ad fits; score 3 at most, they are a second choice): "
                     + "; ".join(also))
    if data["exclude"]:
        lines.append("Always reject (relevant = false): " + "; ".join(data["exclude"]))
    return "\n".join(lines)
