# One Hive R4: skill registry entry format and loader semantics

**Version:** 1 · **Status:** the R4 ↔ R6 contract (release plan §4: "content hash, expiry, and resolution
semantics, so the runtime's loader and the standalone registry never diverge") · **Builds on:** R0 pins
([hive-pin SPEC](https://github.com/1-hive/hive-pin/blob/main/SPEC.md), v1 tree pins)

A skill encodes a method. It does not define organizational truth, task state, identity, authority, routing or
durable memory: those are the record's, the gateway's and the router's. This document fixes three things: what a
skill is, what a registry entry is, and what loading a set of skills does, including every refusal.

## 1. A skill

A skill is a directory in a git repository whose root holds `SKILL.md` in the
[Agent Skills](https://agentskills.io) format, the one Claude Code, Codex, Copilot, Cursor and OpenClaw read:

- YAML frontmatter between `---` lines, then Markdown.
- `name` (required): 1–64 characters of `a-z`, `0-9` and `-`; no leading, trailing or doubled `-`; equal to the
  directory's name.
- `description` (required): 1–1024 characters.
- `license`, `compatibility` (at most 500 characters), `metadata` (a map of strings to strings) and `allowed-tools`
  are optional. Other keys (harness-specific ones) are allowed and kept as they are.
- Everything in the directory is a regular file or directory (R0 v1 tree rules: no symlinks, no submodules).

A skill is installed byte for byte. Nothing is substituted at install time: per-hive values (a handle, a URL) belong
in the agent's configuration, which the skill tells it to read.

## 2. The registry

One JSON file per hive (`skills.json`), kept in git: its history is the skills' version control.

```json
{"version": 1,
 "default_lifetime_days": 90, "max_lifetime_days": 180,
 "repositories": {"hive-runtime": {"fetch_urls": ["https://github.com/1-hive/hive-runtime.git"],
                                   "local_path": "~/.local/share/1-hive/hive-runtime"}},
 "skills": [
   {"name": "hive-worker",
    "pin": {"version": 1, "repository": "hive-runtime", "commit_oid": "sha1:…", "path": "skills/hive-worker",
            "kind": "tree", "object_oid": "sha1:…", "content_digest": "sha256:…"},
    "registered_at": "2026-11-10T23:00:00Z", "expires_at": "2027-02-08T23:00:00Z",
    "roles": ["worker", "reviewer"], "harnesses": [], "conflicts": [], "note": ""}]}
```

- `repositories` is an R0 repository registry (hive-pin SPEC §7) for the repositories skills come from. It is the
  skills registry's own, so registering a skill never changes the record's repository registry.
- An **entry** is an approval of one exact skill for a time:
  - `name`: the skill's `name`; the pin's last path component.
  - `pin`: an R0 **v1 tree pin** of the skill directory. Its `content_digest` (SHA-256 over the canonical tree
    manifest, R0 §11.2) is **the skill's content hash**. Two skills are the same skill exactly when their content
    hashes are equal.
  - `registered_at`, `expires_at`: RFC 3339 UTC. Every entry expires: `expires_at` is after `registered_at` and at
    most `max_lifetime_days` after it. An entry is **active** at time *t* when `t < expires_at`.
  - `roles`, `harnesses`: who loads it when skills are selected by role (§3). Empty means any.
  - `conflicts`: names of skills that must never be loaded together with this one (either side may declare it).
  - `note`: at most 300 characters.
- **A registry is valid** only if no two entries share a name (`DUPLICATE_SKILL`) or a content hash
  (`DUPLICATE_SKILL`; valid skills can't, since `SKILL.md` carries its directory's name, so this guards
  hand-edited files), and every entry is well formed (`INVALID_ENTRY`). Every operation refuses an invalid
  registry. Entries are kept sorted by name.
- **Registering** pins the committed skill directory, validates its `SKILL.md` (§1, `INVALID_SKILL`), and adds the
  entry. A name already registered is refused (`DUPLICATE_SKILL`) unless it **replaces** that entry, keeping its
  roles, harnesses and conflicts unless new ones are given. **Renewing** gives the same content a new `expires_at`.
  **Retiring** removes an entry. **Pruning** removes every expired entry.

## 3. Resolution

A request names skills, or a role and a harness, at a time *t* (default: now). It resolves to a **set**:

- By name: each named entry; an unknown name is refused (`UNKNOWN_SKILL`).
- By role and harness: every entry whose `roles` is empty or contains the role, and whose `harnesses` is empty or
  contains the harness.
- An entry in the set that is not active at *t* is refused (`SKILL_EXPIRED`), never skipped: a worker must not
  silently run without a skill it was meant to have. Renew it, replace it or retire it.
- Two entries in the set that declare a conflict are refused (`SKILL_CONFLICT`).

## 4. Loading

Loading installs a resolved set into one **skills directory** (the harness's own: `.claude/skills/`,
`.agents/skills/`, an OpenClaw workspace's `skills/`), as `<dir>/<name>/`. The loader:

1. resolves the set (§3);
2. verifies every pin against its repository (R0 `verify`; `CONTENT_MISMATCH` and the other R0 codes pass
   through) and materializes it into a staging directory, so a refusal changes nothing installed;
3. refuses a selected name whose directory exists in the skills directory but is not managed by the loader
   (`SKILL_CONFLICT`: an unregistered skill of the same name);
4. then makes the managed skills exactly the set: installs new ones, replaces those whose content hash differs from
   the registry's (a newer version, or an installed copy that was edited: **restored**), and removes managed skills
   no longer in the set (expired, retired or no longer for this role). Unmanaged directories are left alone.

It writes `<dir>/.hive-skills.lock`:

```json
{"version": 1, "loaded_at": "…", "registry_digest": "sha256:…", "set_digest": "sha256:…",
 "skills": [{"name": "hive-worker", "content_digest": "sha256:…", "expires_at": "…", "pin": {…}}]}
```

`registry_digest` is the SHA-256 of the registry file's bytes. `set_digest` is the SHA-256 of the canonical JSON
(sorted keys, no spaces) of `[[name, content_digest], …]` sorted by name: **one digest for the skills an agent
loaded**, for the runtime to pin with the attempt (release plan context capture: "role prompt, R4 skill hashes,
R8 route").

## 5. Drift

Checking a skills directory against its lock and the registry at *t* gives each skill one status:

| status | meaning |
|---|---|
| `ok` | installed bytes match the lock, and the registry still has that content, active |
| `changed` | the installed bytes differ from the lock (edited, or a symlink appeared) |
| `missing` | in the lock, not installed |
| `expired` | its entry expired |
| `superseded` | the registry now has other content under that name, or no longer has the name |
| `unregistered` | a directory with a `SKILL.md` that the loader doesn't manage |

The installed hash is computed from the files on disk as R0 §11.2 computes it from git (mode `100755` when any
execute bit is set, else `100644`), so a skill that was not changed hashes to its pin's `content_digest`. Anything
but `ok` is drift; loading again repairs all of it except `unregistered`.

## 6. Refusal codes

`INVALID_REGISTRY`, `INVALID_ENTRY`, `INVALID_SKILL`, `DUPLICATE_SKILL`, `UNKNOWN_SKILL`, `SKILL_EXPIRED`,
`SKILL_CONFLICT`, `EXPIRY_INVALID`, plus R0's codes from pin, verify and materialize. A refusal is a non-zero
exit with `{"error": {"code", "message"}}` on stdout under `--json`.
