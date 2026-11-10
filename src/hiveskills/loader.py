# SPDX-License-Identifier: GPL-3.0-or-later
"""Loading a resolved set into a skills directory (SPEC.md §4), and checking one for drift (§5)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from hivepin import PinError, materialize, pin_from_dict

from . import skill
from .errors import SkillError, from_pin_error
from .registry import Entry, SkillRegistry, fmt, now

LOCK = ".hive-skills.lock"
LOCK_VERSION = 1


def set_digest(pairs: list[tuple[str, str]]) -> str:
    """One digest for a set of skills: SHA-256 of the canonical JSON of [[name, content_digest], …] by name."""
    data = json.dumps(sorted([list(p) for p in pairs]), separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def read_lock(dest: Path) -> dict:
    p = Path(dest) / LOCK
    if not p.exists():
        return {"version": LOCK_VERSION, "skills": []}
    try:
        lock = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SkillError("INVALID_REGISTRY", f"unreadable lock {p}: {e}") from None
    if not isinstance(lock, dict) or lock.get("version") != LOCK_VERSION or not isinstance(lock.get("skills"), list):
        raise SkillError("INVALID_REGISTRY", f"{p} is not a version {LOCK_VERSION} lock")
    return lock


def _installed_digest(path: Path) -> str | None:
    try:
        return skill.digest(path)
    except (SkillError, OSError):
        return None


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def load(reg: SkillRegistry, dest: Path, *, names: list[str] | None = None, role: str | None = None,
         harness: str | None = None, at: dt.datetime | None = None, offline: bool = False) -> dict:
    """Make the managed skills in `dest` exactly the resolved set. Stages and verifies everything before
    changing anything installed; a refusal leaves `dest` as it was."""
    at = at or now()
    dest = Path(dest)
    chosen = reg.resolve(names, role, harness, at)
    dest.mkdir(parents=True, exist_ok=True)
    managed = {s["name"]: s for s in read_lock(dest)["skills"]}
    for e in chosen:
        target = dest / e.name
        if (target.exists() or target.is_symlink()) and e.name not in managed:
            raise SkillError("SKILL_CONFLICT", f"{target} holds a skill the loader doesn't manage; move it away "
                                               "or register it", skills=[e.name])
    stage = Path(tempfile.mkdtemp(prefix=".hive-skills-stage-", dir=dest))
    report: dict[str, list[str]] = {"installed": [], "updated": [], "restored": [], "unchanged": [], "removed": []}
    try:
        staged: dict[str, Path] = {}
        for e in chosen:
            if _installed_digest(dest / e.name) == e.content_digest:
                report["unchanged"].append(e.name)
                continue
            try:
                materialize(e.pin, stage / e.name, reg.pin_registry(), offline=offline)
            except PinError as err:
                raise from_pin_error(err) from None
            staged[e.name] = stage / e.name / e.pin.path
            skill.validate(staged[e.name], expect_name=e.name)
            if e.name not in managed:
                report["installed"].append(e.name)
            elif managed[e.name].get("content_digest") != e.content_digest:
                report["updated"].append(e.name)
            else:
                report["restored"].append(e.name)
        # Everything verified: now change what is installed.
        for name in sorted(set(managed) - {e.name for e in chosen}):
            _remove(dest / name)
            report["removed"].append(name)
        for name, src in staged.items():
            _remove(dest / name)
            os.replace(src, dest / name)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    skills = [{"name": e.name, "content_digest": e.content_digest, "expires_at": fmt(e.expires_at),
               "pin": e.pin.to_canonical_dict()} for e in chosen]
    lock = {"version": LOCK_VERSION, "loaded_at": fmt(at), "registry_digest": reg.digest(),
            "set_digest": set_digest([(s["name"], s["content_digest"]) for s in skills]), "skills": skills}
    fd, tmp = tempfile.mkstemp(dir=dest, prefix=f"{LOCK}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(lock, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, 0o644)   # mkstemp makes it 0600; the agent may read what it loaded
    os.replace(tmp, dest / LOCK)
    return {"dest": str(dest), "set_digest": lock["set_digest"], "registry_digest": lock["registry_digest"],
            "skills": [{"name": s["name"], "content_digest": s["content_digest"], "expires_at": s["expires_at"]}
                       for s in skills], **report}


def check(reg: SkillRegistry, dest: Path, at: dt.datetime | None = None) -> dict:
    """Each skill's status in `dest` against its lock and the registry (SPEC.md §5). drift is True unless all ok."""
    at = at or now()
    dest = Path(dest)
    lock = read_lock(dest)
    out = []
    for s in lock["skills"]:
        name, locked = s["name"], s.get("content_digest")
        path = dest / name
        e: Entry | None = reg.entries.get(name)
        if not (path.exists() or path.is_symlink()):
            status = "missing"
        elif _installed_digest(path) != locked:
            status = "changed"
        elif e is None or e.content_digest != locked:
            status = "superseded"
        elif not e.active(at):
            status = "expired"
        else:
            status = "ok"
        out.append({"name": name, "status": status, "content_digest": locked,
                    **({"registry_digest": e.content_digest} if e and e.content_digest != locked else {}),
                    **({"expires_at": fmt(e.expires_at)} if e else {})})
    managed = {s["name"] for s in lock["skills"]}
    if dest.is_dir():
        for p in sorted(dest.iterdir()):
            if p.name not in managed and not p.name.startswith(".") and (p / "SKILL.md").exists():
                out.append({"name": p.name, "status": "unregistered", "content_digest": _installed_digest(p)})
    try:
        pin_ok = all(pin_from_dict(s["pin"]) for s in lock["skills"])
    except PinError:
        pin_ok = False
    return {"dest": str(dest), "set_digest": lock.get("set_digest"), "skills": out,
            "drift": not pin_ok or any(s["status"] != "ok" for s in out)}
