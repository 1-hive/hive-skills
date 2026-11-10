# SPDX-License-Identifier: GPL-3.0-or-later
"""hive-skill: the One Hive R4 skill registry and loader (SPEC.md)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, loader, skill
from .errors import EXIT_OK, EXIT_REFUSED, EXIT_USAGE, SkillError
from .registry import SkillRegistry, fmt, now, parse_time


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hive-skill", description=__doc__,
                                epilog="Registry: --registry, else $HIVE_SKILLS_REGISTRY, else ./skills.json.")
    p.add_argument("--version", action="version", version=f"hive-skill {__version__}")
    p.add_argument("--registry", type=Path)
    p.add_argument("--json", action="store_true", help="one JSON object on stdout")
    p.add_argument("--at", help="evaluate expiry at this RFC 3339 time instead of now")
    p.add_argument("--offline", action="store_true", help="no publication checks; read the local clones only")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="create an empty registry")
    s.add_argument("--default-lifetime-days", type=int, default=90)
    s.add_argument("--max-lifetime-days", type=int, default=180)
    s = sub.add_parser("add-repo", help="add (or change) a repository skills come from")
    s.add_argument("id"); s.add_argument("fetch_url"); s.add_argument("--local-path")
    s = sub.add_parser("register", help="pin a committed skill directory and register it")
    s.add_argument("repository"); s.add_argument("path")
    s.add_argument("--commit"); s.add_argument("--expires", help="'<n>d', a date, or an RFC 3339 time")
    s.add_argument("--role", action="append"); s.add_argument("--harness", action="append")
    s.add_argument("--conflicts", action="append"); s.add_argument("--note")
    s.add_argument("--replace", action="store_true", help="register a new version of a registered skill")
    s = sub.add_parser("renew", help="approve the same content for a new period")
    s.add_argument("name"); s.add_argument("--expires")
    s = sub.add_parser("retire", help="remove a skill from the registry")
    s.add_argument("name")
    sub.add_parser("prune", help="remove every expired entry")
    sub.add_parser("list", help="the registered skills and their status")
    for name, hlp in (("resolve", "the set a request resolves to"), ("load", "install the set into a skills directory")):
        s = sub.add_parser(name, help=hlp)
        if name == "load":
            s.add_argument("dest", type=Path)
        s.add_argument("names", nargs="*"); s.add_argument("--role"); s.add_argument("--harness")
    s = sub.add_parser("check", help="drift in a skills directory against its lock and the registry")
    s.add_argument("dest", type=Path)
    s = sub.add_parser("validate", help="check a skill directory's SKILL.md (no registry needed)")
    s.add_argument("dir", type=Path)
    s = sub.add_parser("digest", help="a skill directory's content hash (no registry needed)")
    s.add_argument("dir", type=Path)
    return p


def _registry_path(a) -> Path:
    return a.registry or Path(os.environ.get("HIVE_SKILLS_REGISTRY") or "skills.json")


def _status(e, at) -> str:
    if not e.active(at):
        return "expired"
    days = (e.expires_at - at).days
    return f"active, expires in {days} d" if days < 14 else "active"


def run(a) -> tuple[int, dict, str]:
    at = parse_time(a.at, "--at") if a.at else now()
    if a.cmd == "validate":
        fm = skill.validate(a.dir)
        return EXIT_OK, {"name": fm["name"], "content_digest": skill.digest(a.dir)}, f"{fm['name']}: valid"
    if a.cmd == "digest":
        d = skill.digest(a.dir)
        return EXIT_OK, {"content_digest": d}, d
    path = _registry_path(a)
    if a.cmd == "init":
        SkillRegistry.create(path, a.default_lifetime_days, a.max_lifetime_days)
        return EXIT_OK, {"registry": str(path)}, f"created {path}"
    reg = SkillRegistry.load(path)
    if a.cmd == "add-repo":
        reg.add_repository(a.id, a.fetch_url, a.local_path)
        reg.save()
        return EXIT_OK, {"repository": a.id}, f"repository {a.id} added to {path}"
    if a.cmd == "register":
        e = reg.register(a.repository, a.path, commit=a.commit, expires=a.expires, roles=a.role,
                         harnesses=a.harness, conflicts=a.conflicts, note=a.note, replace_existing=a.replace,
                         offline=a.offline, at=at)
        reg.save()
        return EXIT_OK, e.to_dict(), f"registered {e.name} {e.content_digest[:19]}… until {fmt(e.expires_at)}"
    if a.cmd == "renew":
        e = reg.renew(a.name, a.expires, at)
        reg.save()
        return EXIT_OK, e.to_dict(), f"renewed {e.name} until {fmt(e.expires_at)}"
    if a.cmd == "retire":
        e = reg.retire(a.name)
        reg.save()
        return EXIT_OK, {"retired": e.name}, f"retired {e.name}"
    if a.cmd == "prune":
        gone = reg.prune(at)
        reg.save()
        return EXIT_OK, {"removed": gone}, ("removed " + ", ".join(gone)) if gone else "nothing expired"
    if a.cmd == "list":
        rows = [{**e.to_dict(), "status": _status(e, at)} for e in sorted(reg.entries.values(), key=lambda e: e.name)]
        text = "\n".join(f"{r['name']:<28} {r['pin']['content_digest'][:19]}…  {r['status']:<26} "
                         f"roles={','.join(r['roles']) or '*'} harnesses={','.join(r['harnesses']) or '*'}"
                         for r in rows) or "(no skills registered)"
        return EXIT_OK, {"skills": rows}, text
    if a.cmd == "resolve":
        es = reg.resolve(a.names, a.role, a.harness, at)
        pairs = [(e.name, e.content_digest) for e in es]
        return EXIT_OK, {"skills": [e.to_dict() for e in es], "set_digest": loader.set_digest(pairs)}, \
            "\n".join(f"{n} {d}" for n, d in pairs) or "(empty set)"
    if a.cmd == "load":
        r = loader.load(reg, a.dest, names=a.names, role=a.role, harness=a.harness, at=at, offline=a.offline)
        parts = [f"{k} {', '.join(r[k])}" for k in ("installed", "updated", "restored", "removed") if r[k]]
        return EXIT_OK, r, f"{a.dest}: {'; '.join(parts) or 'unchanged'} (set {r['set_digest'][:19]}…)"
    if a.cmd == "check":
        r = loader.check(reg, a.dest, at)
        text = "\n".join(f"{s['name']:<28} {s['status']}" for s in r["skills"]) or "(no skills)"
        return (EXIT_REFUSED if r["drift"] else EXIT_OK), r, text + ("\ndrift" if r["drift"] else "\nno drift")
    raise SkillError("INTERNAL_ERROR", f"unknown command {a.cmd}")


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    try:
        code, obj, text = run(a)
    except SkillError as e:
        if a.json:
            print(json.dumps({"error": e.to_dict()}))
        else:
            print(f"hive-skill: {e.code}: {e.message}", file=sys.stderr)
        return e.exit_code
    except OSError as e:
        print(json.dumps({"error": {"code": "INTERNAL_ERROR", "message": str(e)}}) if a.json
              else f"hive-skill: INTERNAL_ERROR: {e}", file=sys.stdout if a.json else sys.stderr)
        return 5
    print(json.dumps(obj, ensure_ascii=False) if a.json else text)
    return code


if __name__ == "__main__":
    sys.exit(main())
