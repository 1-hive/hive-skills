# SPDX-License-Identifier: GPL-3.0-or-later
"""A skill directory (SPEC.md §1): its SKILL.md frontmatter, and its content hash computed from disk."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import yaml
from hivepin.canonical import manifest_digest, sha256_hex

from .errors import SkillError

NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
OPTIONAL_LIMITS = {"compatibility": 500}


def frontmatter(text: str) -> dict:
    """The YAML frontmatter of a SKILL.md, between a first line `---` and the next `---` line."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError("INVALID_SKILL", "SKILL.md must start with a '---' frontmatter line")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        raise SkillError("INVALID_SKILL", "SKILL.md frontmatter has no closing '---' line") from None
    try:
        data = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as e:
        raise SkillError("INVALID_SKILL", f"SKILL.md frontmatter is not valid YAML: {e}") from None
    if not isinstance(data, dict):
        raise SkillError("INVALID_SKILL", "SKILL.md frontmatter must be a mapping")
    return data


def check_name(name: object) -> str:
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or not NAME.match(name):
        raise SkillError("INVALID_SKILL", f"skill name must be 1-64 characters of a-z, 0-9 and single '-' "
                                          f"(not at either end): {name!r}")
    return name


def validate(directory: Path, expect_name: str | None = None) -> dict:
    """Validate a skill directory on disk; returns its frontmatter. The name must equal the directory's name
    (or expect_name, for a directory staged under another name)."""
    d = Path(directory)
    md = d / "SKILL.md"
    if md.is_symlink() or not md.is_file():
        raise SkillError("INVALID_SKILL", f"no SKILL.md in {d}")
    try:
        fm = frontmatter(md.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        raise SkillError("INVALID_SKILL", "SKILL.md is not UTF-8") from None
    name = check_name(fm.get("name"))
    want = expect_name or d.name
    if name != want:
        raise SkillError("INVALID_SKILL", f"SKILL.md name {name!r} differs from its directory {want!r}")
    desc = fm.get("description")
    if not isinstance(desc, str) or not 1 <= len(desc.strip()) <= 1024:
        raise SkillError("INVALID_SKILL", f"{name}: description is required, 1-1024 characters")
    for key, limit in OPTIONAL_LIMITS.items():
        v = fm.get(key)
        if v is not None and (not isinstance(v, str) or len(v) > limit):
            raise SkillError("INVALID_SKILL", f"{name}: {key} must be a string of at most {limit} characters")
    meta = fm.get("metadata")
    if meta is not None and (not isinstance(meta, dict)
                             or not all(isinstance(k, str) and isinstance(v, str) for k, v in meta.items())):
        raise SkillError("INVALID_SKILL", f"{name}: metadata must map strings to strings")
    for key in ("license", "allowed-tools"):
        if fm.get(key) is not None and not isinstance(fm[key], str):
            raise SkillError("INVALID_SKILL", f"{name}: {key} must be a string")
    digest(d)   # only regular files and directories
    return fm


def digest(directory: Path) -> str:
    """The content hash of a skill directory on disk: R0's tree content digest (SHA-256 over the canonical
    manifest, hive-pin SPEC §11.2), computed from the files as git would store them. Refuses symlinks and
    anything but regular files and directories."""
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise SkillError("INVALID_SKILL", f"not a directory: {root}")
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            if (Path(dirpath) / name).is_symlink():
                raise SkillError("INVALID_SKILL", f"symlink in skill: {Path(dirpath, name).relative_to(root)}")
        for name in filenames:
            p = Path(dirpath) / name
            st = p.lstat()
            rel = p.relative_to(root).as_posix()
            if not stat.S_ISREG(st.st_mode):
                raise SkillError("INVALID_SKILL", f"not a regular file in skill: {rel}")
            data = p.read_bytes()
            entries.append({"path": rel, "mode": "100755" if st.st_mode & stat.S_IXUSR else "100644",
                            "size": len(data), "digest": sha256_hex(data)})
    return manifest_digest(entries)
