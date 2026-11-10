# SPDX-License-Identifier: GPL-3.0-or-later
"""The skills registry (SPEC.md §2) and resolution (§3)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from hivepin import Pin, PinError, Registry, materialize, mint, pin_from_dict

from . import skill
from .errors import SkillError, from_pin_error

VERSION = 1
ENTRY_KEYS = {"name", "pin", "registered_at", "expires_at", "roles", "harnesses", "conflicts", "note"}
TOP_KEYS = {"version", "default_lifetime_days", "max_lifetime_days", "repositories", "skills"}
LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def fmt(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: object, what: str = "time") -> dt.datetime:
    if not isinstance(s, str):
        raise SkillError("INVALID_ENTRY", f"{what} must be an RFC 3339 UTC string")
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise SkillError("INVALID_ENTRY", f"{what} is not an RFC 3339 time: {s!r}") from None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)


def parse_expiry(s: str, at: dt.datetime) -> dt.datetime:
    """'90d' (from `at`), a date ('2027-02-08', midnight UTC) or a full RFC 3339 time."""
    m = re.fullmatch(r"(\d+)d", s.strip())
    if m:
        return at + dt.timedelta(days=int(m.group(1)))
    try:
        return parse_time(s, "expiry")
    except SkillError:
        raise SkillError("EXPIRY_INVALID", f"expiry must be '<n>d', a date or an RFC 3339 time: {s!r}") from None


@dataclass(frozen=True)
class Entry:
    name: str
    pin: Pin
    registered_at: dt.datetime
    expires_at: dt.datetime
    roles: tuple[str, ...] = ()
    harnesses: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    note: str = ""

    @property
    def content_digest(self) -> str:
        return self.pin.content_digest

    def active(self, at: dt.datetime) -> bool:
        return at < self.expires_at

    def to_dict(self) -> dict:
        return {"name": self.name, "pin": self.pin.to_canonical_dict(), "registered_at": fmt(self.registered_at),
                "expires_at": fmt(self.expires_at), "roles": list(self.roles), "harnesses": list(self.harnesses),
                "conflicts": list(self.conflicts), "note": self.note}

    @classmethod
    def from_dict(cls, d: object) -> Entry:
        if not isinstance(d, dict):
            raise SkillError("INVALID_ENTRY", "an entry must be a JSON object")
        unknown = set(d) - ENTRY_KEYS
        if unknown:
            raise SkillError("INVALID_ENTRY", f"unknown entry keys: {sorted(unknown)}")
        name = d.get("name")
        try:
            skill.check_name(name)
        except SkillError as e:
            raise SkillError("INVALID_ENTRY", e.message) from None
        try:
            pin = pin_from_dict(d.get("pin"))
        except PinError as e:
            raise SkillError("INVALID_ENTRY", f"{name}: bad pin: {e}") from None
        if not isinstance(pin, Pin) or pin.kind != "tree":
            raise SkillError("INVALID_ENTRY", f"{name}: the pin must be an R0 v1 tree pin")
        if PurePosixPath(pin.path).name != name:
            raise SkillError("INVALID_ENTRY", f"{name}: the pinned directory is {pin.path!r}, not .../{name}")
        lists = {}
        for key in ("roles", "harnesses", "conflicts"):
            v = d.get(key, [])
            if not isinstance(v, list) or not all(isinstance(x, str) and LABEL.match(x) for x in v):
                raise SkillError("INVALID_ENTRY", f"{name}: {key} must be a list of short lowercase labels")
            lists[key] = tuple(sorted(set(v)))
        note = d.get("note", "")
        if not isinstance(note, str) or len(note) > 300:
            raise SkillError("INVALID_ENTRY", f"{name}: note must be a string of at most 300 characters")
        reg, exp = parse_time(d.get("registered_at"), f"{name}: registered_at"), \
            parse_time(d.get("expires_at"), f"{name}: expires_at")
        if exp <= reg:
            raise SkillError("INVALID_ENTRY", f"{name}: expires_at must be after registered_at")
        if name in lists["conflicts"]:
            raise SkillError("INVALID_ENTRY", f"{name}: a skill can't conflict with itself")
        return cls(name, pin, reg, exp, lists["roles"], lists["harnesses"], lists["conflicts"], note)


@dataclass
class SkillRegistry:
    path: Path
    default_lifetime_days: int = 90
    max_lifetime_days: int = 180
    repositories: dict = field(default_factory=dict)
    entries: dict[str, Entry] = field(default_factory=dict)

    # ------------------------------------------------------------------ file
    @classmethod
    def create(cls, path: Path, default_lifetime_days: int = 90, max_lifetime_days: int = 180) -> SkillRegistry:
        path = Path(path)
        if path.exists():
            raise SkillError("INVALID_REGISTRY", f"{path} already exists")
        r = cls(path, default_lifetime_days, max_lifetime_days)
        r.check()
        r.save()
        return r

    @classmethod
    def load(cls, path: Path) -> SkillRegistry:
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SkillError("INVALID_REGISTRY", f"no skills registry at {path}") from None
        except (OSError, ValueError) as e:
            raise SkillError("INVALID_REGISTRY", f"cannot read {path}: {e}") from None
        if not isinstance(data, dict) or data.get("version") != VERSION:
            raise SkillError("INVALID_REGISTRY", f"{path}: version must be {VERSION}")
        unknown = set(data) - TOP_KEYS
        if unknown:
            raise SkillError("INVALID_REGISTRY", f"{path}: unknown keys {sorted(unknown)}")
        skills = data.get("skills", [])
        if not isinstance(skills, list):
            raise SkillError("INVALID_REGISTRY", f"{path}: skills must be a list")
        r = cls(path, data.get("default_lifetime_days", 90), data.get("max_lifetime_days", 180),
                data.get("repositories") or {})
        for d in skills:
            e = Entry.from_dict(d)
            if e.name in r.entries:
                raise SkillError("DUPLICATE_SKILL", f"{path}: {e.name} is registered twice")
            r.entries[e.name] = e
        r.check()
        return r

    def check(self) -> None:
        for key in ("default_lifetime_days", "max_lifetime_days"):
            v = getattr(self, key)
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                raise SkillError("INVALID_REGISTRY", f"{key} must be a positive whole number of days")
        if self.default_lifetime_days > self.max_lifetime_days:
            raise SkillError("INVALID_REGISTRY", "default_lifetime_days exceeds max_lifetime_days")
        if not isinstance(self.repositories, dict):
            raise SkillError("INVALID_REGISTRY", "repositories must be an object")
        if self.repositories:
            self.pin_registry()
        seen: dict[str, str] = {}
        for e in self.entries.values():
            if e.content_digest in seen:
                raise SkillError("DUPLICATE_SKILL", f"{e.name} and {seen[e.content_digest]} are the same content "
                                                    f"({e.content_digest[:19]}…)")
            seen[e.content_digest] = e.name
            if e.pin.repository not in self.repositories:
                raise SkillError("INVALID_ENTRY", f"{e.name}: repository {e.pin.repository!r} is not in the "
                                                  "registry's repositories")
            if e.expires_at - e.registered_at > dt.timedelta(days=self.max_lifetime_days):
                raise SkillError("INVALID_ENTRY", f"{e.name}: approved for longer than max_lifetime_days "
                                                  f"({self.max_lifetime_days})")

    def to_dict(self) -> dict:
        return {"version": VERSION, "default_lifetime_days": self.default_lifetime_days,
                "max_lifetime_days": self.max_lifetime_days, "repositories": self.repositories,
                "skills": [self.entries[n].to_dict() for n in sorted(self.entries)]}

    def save(self) -> None:
        self.check()
        data = (json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, self.path)

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.path.read_bytes()).hexdigest()

    def pin_registry(self) -> Registry:
        try:
            return Registry.from_dict({"version": 1, "repositories": self.repositories}, source=self.path)
        except PinError as e:
            raise SkillError("INVALID_REGISTRY", f"repositories: {e}") from None

    def add_repository(self, rid: str, fetch_url: str, local_path: str | None) -> None:
        spec = {"fetch_urls": [fetch_url], **({"local_path": local_path} if local_path else {})}
        self.repositories = {**self.repositories, rid: spec}
        self.check()

    # -------------------------------------------------------------- changes
    def register(self, repository: str, path: str, *, commit: str | None = None, expires: str | None = None,
                 roles: list[str] | None = None, harnesses: list[str] | None = None,
                 conflicts: list[str] | None = None, note: str | None = None, replace_existing: bool = False,
                 offline: bool = False, at: dt.datetime | None = None) -> Entry:
        """Pin the committed skill directory `path` in `repository`, validate it, and add (or replace) its entry."""
        at = at or now()
        name = PurePosixPath(path).name
        skill.check_name(name)
        old = self.entries.get(name)
        if old and not replace_existing:
            raise SkillError("DUPLICATE_SKILL", f"{name} is already registered ({old.content_digest[:19]}…); "
                                                "replace it to register a new version")
        try:
            pin = mint(repository, path, self.pin_registry(), commit=commit, offline=offline, version=1).pin
        except PinError as e:
            raise from_pin_error(e) from None
        if pin.kind != "tree":
            raise SkillError("INVALID_SKILL", f"{path} is a file; a skill is a directory with a SKILL.md")
        stage = Path(tempfile.mkdtemp(prefix=".hive-skill-check-"))
        try:
            materialize(pin, stage / "m", self.pin_registry(), offline=True)
            skill.validate(stage / "m" / pin.path, expect_name=name)
        except PinError as e:
            raise from_pin_error(e) from None
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        for other in self.entries.values():
            if other.name != name and other.content_digest == pin.content_digest:
                raise SkillError("DUPLICATE_SKILL", f"{name} has the same content as the registered {other.name}")
        entry = Entry(name, pin, at, self._expiry(expires, at),
                      tuple(sorted(set(roles))) if roles is not None else (old.roles if old else ()),
                      tuple(sorted(set(harnesses))) if harnesses is not None else (old.harnesses if old else ()),
                      tuple(sorted(set(conflicts))) if conflicts is not None else (old.conflicts if old else ()),
                      note if note is not None else (old.note if old else ""))
        Entry.from_dict(entry.to_dict())   # the same checks as loading the file
        self.entries[name] = entry
        self.check()
        return entry

    def renew(self, name: str, expires: str | None = None, at: dt.datetime | None = None) -> Entry:
        at = at or now()
        e = self._get(name)
        new = replace(e, registered_at=at, expires_at=self._expiry(expires, at))
        self.entries[name] = new
        return new

    def retire(self, name: str) -> Entry:
        return self.entries.pop(self._get(name).name)

    def prune(self, at: dt.datetime | None = None) -> list[str]:
        at = at or now()
        gone = sorted(n for n, e in self.entries.items() if not e.active(at))
        for n in gone:
            del self.entries[n]
        return gone

    def _get(self, name: str) -> Entry:
        if name not in self.entries:
            raise SkillError("UNKNOWN_SKILL", f"{name} is not registered", known=sorted(self.entries))
        return self.entries[name]

    def _expiry(self, expires: str | None, at: dt.datetime) -> dt.datetime:
        exp = parse_expiry(expires, at) if expires else at + dt.timedelta(days=self.default_lifetime_days)
        if exp <= at:
            raise SkillError("EXPIRY_INVALID", f"expiry {fmt(exp)} is not in the future")
        if exp - at > dt.timedelta(days=self.max_lifetime_days):
            raise SkillError("EXPIRY_INVALID", f"expiry {fmt(exp)} is more than max_lifetime_days "
                                               f"({self.max_lifetime_days}) away")
        return exp

    # ------------------------------------------------------------ resolution
    def resolve(self, names: list[str] | None = None, role: str | None = None, harness: str | None = None,
                at: dt.datetime | None = None) -> list[Entry]:
        """The set a request resolves to (SPEC.md §3), sorted by name; refuses unknown, expired and conflicting."""
        at = at or now()
        if names:
            chosen = [self._get(n) for n in dict.fromkeys(names)]
        else:
            chosen = [e for e in self.entries.values()
                      if (not e.roles or role in e.roles) and (not e.harnesses or harness in e.harnesses)]
        expired = sorted(e.name for e in chosen if not e.active(at))
        if expired:
            raise SkillError("SKILL_EXPIRED", f"expired: {', '.join(expired)} (renew, replace or retire them)",
                             skills=expired)
        names_in = {e.name for e in chosen}
        for e in chosen:
            clash = sorted(n for n in names_in if n in e.conflicts
                           or e.name in self.entries.get(n, e).conflicts and n != e.name)
            if clash:
                raise SkillError("SKILL_CONFLICT", f"{e.name} conflicts with {', '.join(clash)}; "
                                                   "they can't be loaded together", skills=[e.name, *clash])
        return sorted(chosen, key=lambda e: e.name)
