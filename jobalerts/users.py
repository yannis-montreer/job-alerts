"""Dashboard accounts besides the admin.

The admin is the dashboard's original password (DASHBOARD_PASSWORD_HASH in .env) and anyone using
the dashboard on this computer. Users are stored in users.json (git-ignored): a username, a
password hash, the profiles (alerts) the admin assigned to them and their dashboard language.
A user only sees and changes their own profiles; see dashboard.USER_ROUTES.
"""
import json
import re
from dataclasses import asdict, dataclass, field

from werkzeug.security import check_password_hash, generate_password_hash

from . import config, profiles

PATH = config.ROOT / "users.json"
ADMIN = "admin"
MIN_PASSWORD = 12
USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{1,29}")


@dataclass
class User:
    id: str
    username: str
    password_hash: str = ""
    profiles: list = field(default_factory=list)
    language: str = ""  # dashboard language ("" = the admin's choice)

    def profile_ids(self) -> list[str]:
        """Assigned profiles that still exist, in the admin's profile order."""
        mine = set(self.profiles)
        return [p.id for p in profiles.all_profiles() if p.id in mine]

    def check(self, password: str) -> bool:
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    def public(self) -> dict:
        return dict(id=self.id, username=self.username, profiles=self.profile_ids())


def load() -> list[User]:
    if not PATH.exists():
        return []
    try:
        fields = set(User.__dataclass_fields__)
        return [User(**{k: v for k, v in u.items() if k in fields})
                for u in json.loads(PATH.read_text(encoding="utf-8")).get("users") or []]
    except (ValueError, TypeError):
        return []


def save(users: list[User]):
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"users": [asdict(u) for u in users]}, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(PATH)


def get(user_id: str) -> User | None:
    return next((u for u in load() if u.id == user_id), None)


def by_username(name: str) -> User | None:
    name = (name or "").strip().lower()
    return next((u for u in load() if u.username == name), None)


def _password(pw: str) -> str:
    if len(pw or "") < MIN_PASSWORD:
        raise ValueError(f"The password needs at least {MIN_PASSWORD} characters")
    return generate_password_hash(pw)


def _profile_ids(ids) -> list[str]:
    known = {p.id for p in profiles.all_profiles()}
    unknown = [i for i in ids or [] if i not in known]
    if unknown:
        raise ValueError("Unknown profile: " + ", ".join(unknown))
    return list(dict.fromkeys(ids or []))


def create(username: str, password: str, profile_ids=()) -> User:
    name = (username or "").strip().lower()
    if not USERNAME.fullmatch(name):
        raise ValueError("Username: 2 to 30 characters, letters, digits, dot, dash or underscore")
    if name == ADMIN:
        raise ValueError('"admin" is the administrator\'s name')
    users = load()
    if any(u.username == name for u in users):
        raise ValueError("This username is taken")
    ids = {u.id for u in users}
    n = 1
    while f"u{n}" in ids:
        n += 1
    user = User(id=f"u{n}", username=name, password_hash=_password(password), profiles=_profile_ids(profile_ids))
    save(users + [user])
    return user


def update(user_id: str, profile_ids=None, password: str | None = None, language: str | None = None) -> User:
    users = load()
    user = next((u for u in users if u.id == user_id), None)
    if user is None:
        raise ValueError("Unknown user")
    if profile_ids is not None:
        user.profiles = _profile_ids(profile_ids)
    if password is not None:
        user.password_hash = _password(password)
    if language is not None:
        if language not in ("en", "fr"):
            raise ValueError("Language must be en or fr")
        user.language = language
    save(users)
    return user


def delete(user_id: str):
    users = load()
    if not any(u.id == user_id for u in users):
        raise ValueError("Unknown user")
    save([u for u in users if u.id != user_id])
