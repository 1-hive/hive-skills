# SPDX-License-Identifier: GPL-3.0-or-later
"""SPEC.md end to end against throwaway git repositories with a bare 'published' remote: SKILL.md validation,
registering (duplicates, replacing, expiry), resolution (role, harness, expired, conflicts), loading (staged,
installs, updates, restores, removes, refuses an unmanaged skill and a pin that doesn't verify, changing nothing),
drift (every status) and the command line."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hiveskills import loader, skill
from hiveskills.cli import main
from hiveskills.errors import SkillError
from hiveskills.registry import SkillRegistry

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
T0 = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", "-C", str(cwd),
                           *args], capture_output=True, text=True, check=True, env=GIT_ENV).stdout.strip()


def skill_md(name: str, desc: str = "Does a thing. Use when a thing is needed.", body: str = "Steps.\n",
             extra: str = "") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n{extra}---\n\n# {name}\n\n{body}"


class Hive:
    """A source repository of skills, published to a bare remote, and a skills registry for it."""

    def __init__(self, tmp: Path):
        self.tmp, self.remote, self.work = tmp, tmp / "remote.git", tmp / "src"
        git(tmp, "init", "-q", "--bare", str(self.remote))
        git(tmp, "clone", "-q", str(self.remote), str(self.work))
        self.reg_path = tmp / "skills.json"
        self.reg = SkillRegistry.create(self.reg_path)
        self.reg.add_repository("src", f"file://{self.remote}", str(self.work))
        self.reg.save()

    def write(self, rel: str, text: str, mode: int | None = None) -> None:
        p = self.work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if mode:
            p.chmod(mode)

    def publish(self, msg: str = "c") -> str:
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", msg)
        git(self.work, "push", "-q", "origin", "main")
        return git(self.work, "rev-parse", "HEAD")

    def reload(self) -> SkillRegistry:
        self.reg = SkillRegistry.load(self.reg_path)
        return self.reg


@pytest.fixture
def hive(tmp_path: Path) -> Hive:
    h = Hive(tmp_path)
    h.write("skills/hive-worker/SKILL.md", skill_md("hive-worker", body="Report, then post a result.\n"))
    h.write("skills/hive-worker/scripts/wait.py", "print('wait')\n", 0o755)
    h.write("skills/review-notes/SKILL.md", skill_md("review-notes"))
    h.write("skills/codex-only/SKILL.md", skill_md("codex-only", extra="metadata:\n  owner: hive\n"))
    h.publish()
    return h


def code(fn, *a, **k) -> str:
    with pytest.raises(SkillError) as ei:
        fn(*a, **k)
    return ei.value.code


# ------------------------------------------------------------------ SKILL.md
def test_validate_skill_md(tmp_path: Path):
    d = tmp_path / "good-skill"
    d.mkdir()
    (d / "SKILL.md").write_text(skill_md("good-skill", extra="license: MIT\nuser-invocable: false\n"))
    assert skill.validate(d)["name"] == "good-skill"   # harness-specific keys are kept, not refused
    for name, text in (("other-name", skill_md("good-skill")), ("x", "no frontmatter\n"),
                       ("Bad-Name", skill_md("Bad-Name")), ("a--b", skill_md("a--b")),
                       ("no-desc", "---\nname: no-desc\n---\nbody\n"), ("long", skill_md("long", "d" * 1025)),
                       ("meta", skill_md("meta", extra="metadata:\n  n: 1\n"))):
        bad = tmp_path / name
        bad.mkdir()
        (bad / "SKILL.md").write_text(text)
        assert code(skill.validate, bad) == "INVALID_SKILL", name
    os.symlink("/etc/passwd", d / "link")
    assert code(skill.validate, d) == "INVALID_SKILL"


# ---------------------------------------------------------------- registering
def test_register_pins_content_and_refuses_duplicates(hive: Hive):
    e = hive.reg.register("src", "skills/hive-worker", roles=["worker"], at=T0)
    hive.reg.save()
    assert e.pin.kind == "tree" and e.expires_at == T0 + dt.timedelta(days=90)
    assert skill.digest(hive.work / "skills/hive-worker") == e.content_digest   # disk hash == pin's content hash
    assert code(hive.reg.register, "src", "skills/hive-worker", at=T0) == "DUPLICATE_SKILL"
    # The same skill moved to another path: a replacement with the same content hash. (Two names can't share
    # content: SKILL.md carries its directory's name.)
    shutil.copytree(hive.work / "skills/hive-worker", hive.work / "other/hive-worker")
    hive.publish()
    moved = hive.reg.register("src", "other/hive-worker", replace_existing=True, at=T0)
    assert moved.content_digest == e.content_digest and moved.pin.path == "other/hive-worker"
    # A new version replaces the entry and keeps its roles.
    hive.write("skills/hive-worker/SKILL.md", skill_md("hive-worker", body="Report, post, wait for review.\n"))
    hive.publish()
    e2 = hive.reg.register("src", "skills/hive-worker", replace_existing=True, expires="30d", at=T0)
    assert e2.content_digest != e.content_digest and e2.roles == ("worker",)
    assert e2.expires_at == T0 + dt.timedelta(days=30)
    hive.reg.save()
    assert hive.reload().entries["hive-worker"].content_digest == e2.content_digest


def test_register_refuses_bad_skills_and_expiry(hive: Hive):
    hive.write("skills/misnamed/SKILL.md", skill_md("not-misnamed"))
    hive.write("skills/hive-worker/extra.txt", "uncommitted\n")
    hive.publish()
    assert code(hive.reg.register, "src", "skills/misnamed", at=T0) == "INVALID_SKILL"
    assert code(hive.reg.register, "src", "skills/review-notes/SKILL.md", at=T0) in ("INVALID_SKILL", "INVALID_PATH")
    assert code(hive.reg.register, "src", "skills/review-notes", expires="200d", at=T0) == "EXPIRY_INVALID"
    assert code(hive.reg.register, "src", "skills/review-notes", expires="2026-11-01", at=T0) == "EXPIRY_INVALID"
    hive.write("skills/review-notes/SKILL.md", skill_md("review-notes", body="edited, not committed\n"))
    assert code(hive.reg.register, "src", "skills/review-notes", at=T0) == "DIRTY_PATH"   # R0's code passes through


def test_registry_file_refuses_duplicates_and_bad_entries(hive: Hive):
    hive.reg.register("src", "skills/hive-worker", at=T0)
    hive.reg.save()
    data = json.loads(hive.reg_path.read_text())
    data["skills"].append(data["skills"][0])
    hive.reg_path.write_text(json.dumps(data))
    assert code(SkillRegistry.load, hive.reg_path) == "DUPLICATE_SKILL"
    data["skills"] = [{**data["skills"][0], "expires_at": "2026-01-01T00:00:00Z"}]
    hive.reg_path.write_text(json.dumps(data))
    assert code(SkillRegistry.load, hive.reg_path) == "INVALID_ENTRY"
    data["skills"] = [{**data["skills"][0], "expires_at": "2027-11-10T00:00:00Z"}]   # past max_lifetime_days
    hive.reg_path.write_text(json.dumps(data))
    assert code(SkillRegistry.load, hive.reg_path) == "INVALID_ENTRY"


# ------------------------------------------------------------------ resolution
def test_resolution(hive: Hive):
    r = hive.reg
    r.register("src", "skills/hive-worker", roles=["worker", "reviewer"], at=T0)
    r.register("src", "skills/review-notes", roles=["reviewer"], expires="10d", at=T0)
    r.register("src", "skills/codex-only", harnesses=["codex"], at=T0)
    names = lambda es: [e.name for e in es]
    assert names(r.resolve(role="worker", harness="claude-code", at=T0)) == ["hive-worker"]
    assert names(r.resolve(role="reviewer", harness="codex", at=T0)) == ["codex-only", "hive-worker", "review-notes"]
    assert names(r.resolve(["review-notes"], at=T0)) == ["review-notes"]
    assert code(r.resolve, ["nope"], at=T0) == "UNKNOWN_SKILL"
    later = T0 + dt.timedelta(days=11)
    assert code(r.resolve, role="reviewer", at=later) == "SKILL_EXPIRED"   # refused, never skipped
    assert names(r.resolve(role="worker", at=later)) == ["hive-worker"]
    r.renew("review-notes", "20d", at=later)
    assert names(r.resolve(role="reviewer", harness="claude-code", at=later)) == ["hive-worker", "review-notes"]
    assert r.prune(T0 + dt.timedelta(days=35)) == ["review-notes"]   # renewed to day 31
    r.entries.clear()
    r.register("src", "skills/hive-worker", at=T0)
    r.register("src", "skills/review-notes", conflicts=["hive-worker"], at=T0)
    assert code(r.resolve, at=T0) == "SKILL_CONFLICT"
    assert code(r.resolve, ["hive-worker", "review-notes"], at=T0) == "SKILL_CONFLICT"   # either side declares it
    assert names(r.resolve(["hive-worker"], at=T0)) == ["hive-worker"]


# -------------------------------------------------------------------- loading
def test_load_install_update_restore_remove(hive: Hive, tmp_path: Path):
    r = hive.reg
    r.register("src", "skills/hive-worker", roles=["worker"], at=T0)
    r.register("src", "skills/review-notes", roles=["worker"], at=T0)
    r.save()
    dest = tmp_path / "task" / ".claude" / "skills"
    rep = loader.load(r, dest, role="worker", at=T0)
    assert rep["installed"] == ["hive-worker", "review-notes"] and not rep["unchanged"]
    assert (dest / "hive-worker/scripts/wait.py").stat().st_mode & 0o100
    assert (dest / loader.LOCK).stat().st_mode & 0o777 == 0o644
    assert skill.digest(dest / "hive-worker") == r.entries["hive-worker"].content_digest
    lock = json.loads((dest / loader.LOCK).read_text())
    assert lock["set_digest"] == rep["set_digest"] == loader.set_digest(
        [(e.name, e.content_digest) for e in r.entries.values()])
    assert loader.load(r, dest, role="worker", at=T0)["unchanged"] == ["hive-worker", "review-notes"]
    # Edited in place: restored. A new version: updated. Retired: removed.
    (dest / "hive-worker/SKILL.md").write_text(skill_md("hive-worker", body="Skip the review.\n"))
    hive.write("skills/review-notes/SKILL.md", skill_md("review-notes", body="v2\n"))
    hive.publish()
    r.register("src", "skills/review-notes", replace_existing=True, at=T0)
    rep = loader.load(r, dest, role="worker", at=T0)
    assert rep["restored"] == ["hive-worker"] and rep["updated"] == ["review-notes"], rep
    assert "Skip the review" not in (dest / "hive-worker/SKILL.md").read_text()
    r.retire("review-notes")
    rep = loader.load(r, dest, role="worker", at=T0)
    assert rep["removed"] == ["review-notes"] and not (dest / "review-notes").exists()
    assert [s["name"] for s in json.loads((dest / loader.LOCK).read_text())["skills"]] == ["hive-worker"]


def test_load_refuses_and_changes_nothing(hive: Hive, tmp_path: Path):
    r = hive.reg
    r.register("src", "skills/hive-worker", roles=["worker"], at=T0)
    r.register("src", "skills/review-notes", roles=["worker"], at=T0)
    dest = tmp_path / "skills"
    (dest / "review-notes").mkdir(parents=True)
    (dest / "review-notes/SKILL.md").write_text(skill_md("review-notes", body="someone else's\n"))
    assert code(loader.load, r, dest, role="worker", at=T0) == "SKILL_CONFLICT"   # unmanaged skill in the way
    assert not (dest / "hive-worker").exists() and not (dest / loader.LOCK).exists()
    shutil.rmtree(dest / "review-notes")
    loader.load(r, dest, role="worker", at=T0)
    before = (dest / loader.LOCK).read_text()
    # The repository's history was rewritten under the pin: verification fails, nothing installed changes.
    hive.write("skills/review-notes/SKILL.md", skill_md("review-notes", body="v2\n"))
    hive.publish()
    r.register("src", "skills/review-notes", replace_existing=True, at=T0)
    good = r.entries["review-notes"]
    from dataclasses import replace
    r.entries["review-notes"] = replace(good, pin=replace(good.pin, content_digest="sha256:" + "0" * 64))
    assert code(loader.load, r, dest, role="worker", at=T0) == "CONTENT_MISMATCH"
    assert (dest / loader.LOCK).read_text() == before and "v2" not in (dest / "review-notes/SKILL.md").read_text()
    assert not [p for p in dest.iterdir() if p.name.startswith(".hive-skills-stage-")]
    r.entries["review-notes"] = replace(good, expires_at=T0 + dt.timedelta(days=1))
    assert code(loader.load, r, dest, role="worker", at=T0 + dt.timedelta(days=2)) == "SKILL_EXPIRED"
    assert (dest / loader.LOCK).read_text() == before


# ---------------------------------------------------------------------- drift
def test_check_every_status(hive: Hive, tmp_path: Path):
    r = hive.reg
    for n in ("hive-worker", "review-notes", "codex-only"):
        r.register("src", f"skills/{n}", expires="10d" if n == "codex-only" else None, at=T0)
    dest = tmp_path / "skills"
    loader.load(r, dest, at=T0)
    assert not loader.check(r, dest, T0)["drift"]
    (dest / "hive-worker/SKILL.md").write_text(skill_md("hive-worker", body="edited\n"))
    shutil.rmtree(dest / "review-notes")
    (dest / "stray").mkdir()
    (dest / "stray/SKILL.md").write_text(skill_md("stray"))
    st = lambda at: {s["name"]: s["status"] for s in loader.check(r, dest, at)["skills"]}
    assert st(T0) == {"hive-worker": "changed", "review-notes": "missing", "codex-only": "ok", "stray": "unregistered"}
    assert st(T0 + dt.timedelta(days=11))["codex-only"] == "expired"
    hive.write("skills/codex-only/SKILL.md", skill_md("codex-only", body="v2\n"))
    hive.publish()
    r.register("src", "skills/codex-only", replace_existing=True, at=T0)
    assert st(T0)["codex-only"] == "superseded"
    r.retire("codex-only")
    assert st(T0)["codex-only"] == "superseded"
    # Loading again repairs all but the unregistered one.
    r.register("src", "skills/codex-only", at=T0)
    loader.load(r, dest, at=T0)
    assert set(st(T0).values()) == {"ok", "unregistered"}


# ----------------------------------------------------------------- command line
def test_cli(hive: Hive, tmp_path: Path, capsys):
    reg = ["--registry", str(hive.reg_path), "--json"]
    run = lambda *a: (main([*reg, *a]), json.loads(capsys.readouterr().out))
    rc, out = run("--at", "2026-11-10T12:00:00Z", "register", "src", "skills/hive-worker", "--role", "worker")
    assert rc == 0 and out["name"] == "hive-worker" and out["expires_at"] == "2027-02-08T12:00:00Z"
    rc, out = run("register", "src", "skills/hive-worker")
    assert rc == 3 and out["error"]["code"] == "DUPLICATE_SKILL"
    rc, out = run("load", str(tmp_path / "d"), "--role", "worker")
    assert rc == 0 and out["installed"] == ["hive-worker"]
    rc, out = run("check", str(tmp_path / "d"))
    assert rc == 0 and out["drift"] is False
    (tmp_path / "d/hive-worker/SKILL.md").write_text("x")
    rc, out = run("check", str(tmp_path / "d"))
    assert rc == 3 and out["drift"] is True
    rc, out = run("digest", str(hive.work / "skills/hive-worker"))
    assert out["content_digest"] == hive.reload().entries["hive-worker"].content_digest
    rc, out = run("list")
    assert rc == 0 and out["skills"][0]["status"].startswith("active")
    rc, out = run("--at", "2027-03-01T00:00:00Z", "resolve", "--role", "worker")
    assert rc == 3 and out["error"]["code"] == "SKILL_EXPIRED"
    rc, out = run("--at", "2027-03-01T00:00:00Z", "prune")
    assert out["removed"] == ["hive-worker"]
