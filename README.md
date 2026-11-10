# hive-skills

One Hive **R4**: a skill registry with content-hashed, expiring entries, and a loader that refuses duplicates and
conflicts. [`SPEC.md`](SPEC.md) is the entry format and the loader's semantics (the R4 ↔ R6 contract).

- **Skills are [Agent Skills](https://agentskills.io):** a directory with a `SKILL.md`, the format Claude Code,
  Codex, Copilot, Cursor and OpenClaw read. Nothing here is a new skill format.
- **What R4 adds is the part registries leave out:** an entry approves one exact skill (an
  [R0](https://github.com/1-hive/hive-pin) tree pin; its content digest is the skill's content hash) until a date.
  The loader installs exactly the approved set into a harness's skills directory, verified against the pins, and
  refuses an unknown, expired or conflicting skill, a pin that no longer verifies, and an unregistered skill of the
  same name in the way. A refusal changes nothing installed.
- **Drift is visible:** `check` compares an installed set with its lock and the registry: `changed` (edited in
  place), `missing`, `expired`, `superseded` (a newer version registered, or retired) and `unregistered`. Loading
  again repairs all but the last.
- **Standalone:** one JSON file per hive, kept in git (its history is the skills' version control), and one
  command. No record, gateway or runtime is needed; [hive-runtime](https://github.com/1-hive/hive-runtime) uses it
  to load each task's skills ([`docs/skills.md`](https://github.com/1-hive/hive-runtime/blob/main/docs/skills.md)).

## Install

```
uv tool install git+https://github.com/1-hive/hive-skills@v1.0.0
```

## Use

```
hive-skill init                                                  # skills.json (or --registry PATH)
hive-skill add-repo hive-runtime https://github.com/1-hive/hive-runtime.git --local-path ~/src/hive-runtime
hive-skill register hive-runtime skills/hive-worker --role worker --role reviewer --expires 90d
hive-skill list
hive-skill load ~/task/.agents/skills --role worker --harness claude-code
hive-skill check ~/task/.agents/skills                           # exit 3 on drift
hive-skill register hive-runtime skills/hive-worker --commit v2 --replace    # a new version
hive-skill renew hive-worker --expires 90d                       # the same content, approved again
hive-skill retire hive-worker
hive-skill prune                                                 # drop expired entries
hive-skill validate ./my-skill        # SKILL.md rules, no registry needed
hive-skill digest ./my-skill          # the content hash, as R0 computes it
```

`--json` prints one object (a refusal: `{"error": {"code", "message"}}`), `--at` evaluates expiry at another time,
and `--offline` skips publication checks (registering checks by default that the commit is published).

Where harnesses look for skills: Claude Code `.claude/skills/`, Codex `.agents/skills/` (and `~/.agents/skills`),
OpenClaw `<workspace>/skills/`. Load into one and link the others to it, as hive-runtime does.

## Tests

```
uv run --extra test pytest
```

GPL-3.0-or-later.
