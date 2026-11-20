# commons Implementation Plan

> For Hermes: use the subagent-driven-development skill to implement this plan task by task. One fresh subagent per task, then a spec review and a code-quality review before the next task starts.

Goal: build the `commons` Hermes plugin described in `docs/specs/2026-11-20-commons-design.md` (approved by Chris 2026-11-20).

Architecture: a pure-Python core (`commons/core/`) that works on plain folders and has no Hermes imports, so it is fully testable with synthetic fleets in tmp dirs. A thin Hermes adapter (`commons/hermes_adapter.py`) is the only module that imports Hermes, and only public-API modules. `commons/__init__.py` wires hooks, the CLI and the slash command.

Tech stack: Python >= 3.11, stdlib plus PyYAML (Hermes already ships it), git CLI, pytest. Developed against Hermes v0.23.1 (c9c87fc).

Repo: `~/hermes-commons` (local only until Phase 6; never commit Chris's real skills).

Style rules for every task: no em-dashes anywhere (code, comments, tests, commits); plain commit messages; no network calls; nothing is deleted, only moved to a backup.

---

## Facts the tasks rely on (verified in Hermes source, 2026-11-20)

- Plugin layout: `plugin.yaml` + `__init__.py` exposing `register(ctx)`.
- `ctx.register_cli_command(name, help, setup_fn, handler_fn=None, description="")`: `setup_fn(subparser)`, `handler_fn(args)` (hermes_cli/plugins_context.py:444).
- `ctx.register_command(name, handler, description="", args_hint="")`: slash command, `handler(raw_args: str) -> str | None` (:458).
- `ctx.register_hook(hook_name, callback)` (:753). `on_skill_lifecycle` kwargs: `action` ("created" | "edited" | "patched" | "loaded" | "installed" ...), `skill_name`, `provenance`, `task_id`, `session_id`, `use_count`, `reused`, `reuse_after_patch` (tools/skill_usage.py:470). Deletes do not fire it.
- Skill precedence: project > local > create_dir > external_dirs (agent/skill_utils.py:402).
- Bundled origin hashes: `<profile>/skills/.bundled_manifest`, lines `name:md5`, compared with `tools.skills_sync.dir_hash` (MD5).
- Hub installs: `<profile>/skills/.hub/lock.json`, `{"installed": {name: {install_path, content_hash, ...}}}`.
- Profiles: default root `hermes_constants.get_default_hermes_root()`; named profiles are `<root>/profiles/<name>/` with a config.yaml; skip ones where `hermes_constants.named_profile_is_deleted(path)` is true.
- Script-only cron scripts must live under `<HERMES_HOME>/scripts/`.
- Plugin skills registered with `ctx.register_skill` ARE indexed (category `plugin`, name `commons:commons`) in profiles where the plugin is enabled (agent/prompt_builder.py `_plugin_skill_prompt_rows`); plugins/index.md saying otherwise is stale. The commons skill ships in the plugin.

---

## Phase 0: Skeleton

### Task 0.1: Repo skeleton and test runner

Files: create `pyproject.toml`, `LICENSE` (MIT, "Christopher Ko"), `commons/__init__.py` (empty `register(ctx): pass`), `commons/core/__init__.py`, `tests/__init__.py`, `.gitignore` (`__pycache__/`, `.pytest_cache/`, `*.egg-info/`).

pyproject: name `hermes-commons`, version `0.1.0`, `requires-python = ">=3.11"`, dependencies `["PyYAML>=6"]`, optional `test = ["pytest>=8"]`, `[tool.pytest.ini_options] testpaths = ["tests"]`.

Verify: `python -m venv .venv && .venv/bin/pip install -e '.[test]' && .venv/bin/pytest` -> "no tests ran", exit 5. Commit "Add project skeleton".

### Task 0.2: plugin.yaml

Create `plugin.yaml`:

```yaml
name: commons
version: "0.1.0"
description: "One live skill library for every Hermes profile: setup, drift checks, per-edit git history."
author: Christopher Ko
requires_hermes: ">=0.23.0"
provides_hooks:
  - on_skill_lifecycle
  - on_session_start
provides_tools: []
```

Verify: `python -c "import yaml;print(yaml.safe_load(open('plugin.yaml'))['name'])"` -> `commons`. Commit.

### Task 0.3: Synthetic fleet fixture

Files: `tests/fleet.py`, `tests/conftest.py`.

`tests/fleet.py` provides `make_fleet(tmp_path, spec) -> Path` where `spec` is a dict `{profile: {skill_rel_path: files_dict | "symlink:<target>"}}` plus optional keys `"_bundled": {profile: {name: content_of_origin}}` (writes `.bundled_manifest` with the md5 of the origin tree computed by a local copy of the dir_hash algorithm) and `"_hub": {profile: {name: install_path}}` (writes `.hub/lock.json`). Profile `default` is written at the root (`root/config.yaml`, `root/skills/`); others at `root/profiles/<p>/`. Every config.yaml starts as `{"skills": {}, "plugins": {"enabled": []}}`.

`conftest.py` fixture `fleet` returns a builder; fixture `classic_fleet` builds the canonical 4-profile case:
- `same` identical in all 4;
- `contained` where device's copy has every line of the others plus extra lines;
- `conflict` where default and device each have unique lines;
- `bundled-pristine` matching its manifest hash in all 4;
- `bundled-edited` differing from its manifest hash in pikvm and pocket;
- `hubskill` recorded in the hub lock, untouched;
- `linked` a symlink in default;
- `dup` present twice inside device (`a/dup`, `b/dup`) with different content.

Test `tests/test_fleet_fixture.py`: the classic fleet has 4 config.yaml files and `profiles/device/skills/a/dup/SKILL.md` exists. Run, pass, commit.

---

## Phase 1: Read-only core (scan, group, status)

### Task 1.1: Fleet discovery (`commons/core/fleet.py`)

```python
@dataclass(frozen=True)
class Profile:
    name: str          # "default" for the root
    home: Path         # HERMES_HOME of the profile
    config: dict       # parsed config.yaml ({} if missing)

    @property
    def skills_dir(self) -> Path: return self.home / "skills"
    def create_dir(self) -> Path | None: ...   # expanduser'd skills.create_dir or None
    def external_dirs(self) -> list[Path]: ... # expanduser'd list, [] if unset
    def plugins_enabled(self) -> list[str]: ...

def discover(root: Path, is_deleted=lambda p: False) -> list[Profile]
```

`discover` returns default first, then named profiles sorted by name, skipping `is_deleted(home)`. Tests: classic fleet gives 4 profiles in order default, device, pikvm, pocket; a profile dir without config.yaml is skipped; a deleted profile is skipped; `create_dir()` expands `~`.

### Task 1.2: Skill walk and provenance (`commons/core/scan.py`)

```python
@dataclass(frozen=True)
class SkillCopy:
    profile: str
    name: str            # frontmatter name, else folder name
    rel: str             # path relative to the profile skills dir, POSIX
    path: Path
    kind: str            # "own" | "bundled-pristine" | "bundled-edited" | "hub-pristine" | "hub-edited" | "symlink"
    tree_hash: str       # sha256 over sorted (relpath, bytes), ignoring __pycache__, .git, *.pyc
    origin_hash: str | None   # manifest or hub hash when kind starts with bundled/hub

def scan_profile(p: Profile, md5_dir_hash) -> list[SkillCopy]
```

Walk: every folder containing SKILL.md under `p.skills_dir`, skipping `.hub`, `.archive`, `.git`, dotted dirs and anything below another skill folder. `md5_dir_hash` is injected (the adapter passes `tools.skills_sync.dir_hash`; tests pass the fixture's copy). Frontmatter `name:` read with a small parser (`---` block, `yaml.safe_load`), falling back to the folder name on any error.

Tests on classic fleet: kinds are classified as listed in Task 0.3; `dup` yields two copies in device; a broken frontmatter falls back to the folder name.

### Task 1.3: Grouping and containment (`commons/core/group.py`)

```python
@dataclass
class Group:
    name: str
    copies: list[SkillCopy]
    status: str   # "identical" | "contained" | "conflict" | "stays-local" | "in-profile-dup"
    keep: SkillCopy | None        # superset copy for contained / any copy for identical
    unique_lines: dict[str, int]  # per copy id (profile:rel), for conflicts
    taken_over: bool              # any copy is bundled-edited or hub-edited

def group(copies: list[SkillCopy]) -> list[Group]
```

Rules:
- All copies symlink, bundled-pristine or hub-pristine -> `stays-local`.
- Two copies of one name inside one profile with different hashes -> `in-profile-dup` (reported, never auto-resolved).
- All remaining copies share one tree_hash -> `identical`.
- Containment: normalize each text file line (collapse whitespace, skip lines shorter than 4 chars and the trivial regex `^(version:|updated:|last_updated:|---$|```\w*$|#+\s*$|[-*|>]\s*$)`); copy A is contained in B when A's normalized line set is a subset of B's and every non-text file in A exists in B. If after dropping contained copies exactly one remains -> `contained` with `keep` = that copy.
- Otherwise -> `conflict`, with `unique_lines` per surviving copy.

Tests: each classic-fleet group gets the expected status; containment ignores a changed `version:` line; a binary file present only in the smaller copy makes it a conflict.

### Task 1.4: Shadow resolution (`commons/core/resolve.py`)

`resolve(profile, shared_root, project_dirs=()) -> dict[name, Resolution]` where `Resolution(winner_tier, winner_path, shadowed: list[(tier, path)])`, tiers in Hermes order project < local < create_dir < external. Use the same name rule as scan. Tests: a local copy of a shared skill shadows it; an external copy loses to shared; two different copies in the same tier are reported as `ambiguous`.

### Task 1.5: Status report (`commons/core/report.py`)

`status(profiles, shared_root) -> StatusReport` (per profile: counts by source, list of shadowed names, index size estimate = sum of `len("  - name: description")` for winners) and `render_status(report, profile=None, skill=None) -> str` (plain text, no colors). Snapshot-style tests with `assert "shadowed: 1" in out` style checks (not full-text snapshots).

Phase 1 gate: `pytest -q` all green, `ruff check commons tests` clean (add ruff to test extras). Commit after each task.

---

## Phase 2: Safe write layer

### Task 2.1: Locked git (`commons/core/gitrepo.py`)

`GitRepo(path)` with `init()`, `is_repo()`, `status_porcelain()`, `commit_paths(paths, message) -> str | None` (returns sha, None if nothing to commit), `log(path=None, n=20)`, `revert(sha)`. Every call runs `git -C <path>` with `GIT_TERMINAL_PROMPT=0`, `-c core.hooksPath=/dev/null`, a fixed author `commons <commons@localhost>` unless `user.name` is set in the repo, and holds an exclusive `fcntl.flock` on `<path>/.commons/lock` for the whole operation (timeout 10 s, then raise `LockTimeout`). Tests: commit returns a sha; empty commit returns None; two processes (`multiprocessing`) committing different files both land, `git fsck` clean.

### Task 2.2: Backups (`commons/core/backup.py`)

`create_backup(shared_root, profiles, label) -> BackupId` copies each profile's `config.yaml` and whole `skills/` (preserving symlinks, `shutil.copytree(symlinks=True)`) to `<shared_root>/.commons/backups/<UTC ts>-<label>/<profile>/`, writes `manifest.json` (profiles, file count, sha256 of each config). `list_backups()`, `verify_backup(id)`. Tests: backup of classic fleet round-trips byte for byte (compare tree hashes).

### Task 2.3: Config writer interface (`commons/core/configio.py`)

`ConfigWriter` protocol with `set(profile, key, value)` and `get(profile, key)`. Implementations: `HermesCliWriter` (runs `hermes -p <name> config set <key> <yaml-value>`; default profile uses no `-p`) in the adapter phase, and `YamlFileWriter` (load, set dotted key, dump) used in tests. Tests use only `YamlFileWriter`. Reason: `hermes config set` preserves comments and formatting the way users expect.

### Task 2.4: Move-not-delete helper (`commons/core/moves.py`)

`retire(path, backup_dir) -> Path` moves a folder into `backup_dir/retired/<profile>/<rel>`, refuses to overwrite, returns the new location. Test: retiring twice to the same target raises.

Phase 2 gate: full suite green.

---

## Phase 3: Setup (proposal, apply, verify, restore)

### Task 3.1: Proposal model (`commons/core/proposal.py`)

`build_proposal(profiles, groups, shared_root, cron_jobs) -> Proposal` containing: shared root path; `moves` (group name, chosen source copy, target rel under shared root; target rel follows the copy's rel in the profile furthest ahead); `conflicts` with default action `merge`; `stays_local`; `in_profile_dups`; `config_changes` per profile (`skills.create_dir`, add `commons` to `plugins.enabled`); `cron_rewrites` (job id, old path, new path) only where the new file will exist; `taken_over` entries with origin hashes. `Proposal.to_json()` / `from_json()` and `render_markdown()`. `apply_choice(proposal, "S=keep:P" | "S=merge")`.

Tests: classic fleet proposal moves `same`, `contained` (device copy), `bundled-edited`; lists `conflict` as merge; leaves `bundled-pristine`, `hubskill`, `linked` local; flags `dup`; JSON round-trip equality; `keep:device` on `conflict` turns it into a move.

### Task 3.2: Apply (`commons/core/apply.py`)

`apply(proposal, profiles, writer, now) -> ApplyResult` in this order, stopping at the first failure and returning what was done:
1. `create_backup(..., "setup")`.
2. `git init` the shared root if needed; write `.gitignore` with `.commons/backups/`, `.commons/staging/`, `.commons/lock`.
3. Copy each move's source into the shared root (`copytree`, refuse if target exists with different content).
4. (removed: the commons skill ships in the plugin, setup writes nothing for it)
5. Commit "commons setup <ts>".
6. For each profile: `writer.set(p, "skills.create_dir", shared_root)`; add `commons` to `plugins.enabled`.
7. Retire every profile's local copy of a moved skill into the backup's `retired/`.
8. Apply cron rewrites (Task 3.4 module).
9. Record taken-over origins in `.commons/config.json`.

Tests on classic fleet with `YamlFileWriter`: after apply, every profile resolves `same`, `contained`, `bundled-edited` from the shared root with no shadows; `conflict` still resolves from each profile's own copy; config has create_dir and plugin enabled; retired copies exist in the backup; nothing missing (every name available before is available after).

### Task 3.3: Verify (`commons/core/verify.py`)

`verify(profiles_before_names, profiles, shared_root) -> list[str]` returns problems: a name available before that is not resolvable now; any shadow of a shared skill not recorded as intentional; same-tier ambiguity. Test: injecting a stray local copy after apply produces exactly one problem.

### Task 3.4: Cron and skill path rewrites (`commons/core/paths.py`)

`find_stale_paths(profiles, shared_root, moved: dict[old_abs, new_abs]) -> list[Finding]` scans `<home>/cron/jobs.json` prompt strings and every `*.md` in the shared root for old absolute or `~`-form skill paths. `rewrite(findings)` replaces only when the new path exists, writing jobs.json atomically (tmp file + `os.replace`) after a copy into the backup. Tests: one cron prompt and one reference file rewritten; a path whose target is missing is reported, not rewritten.

### Task 3.5: Restore (`commons/core/restore.py`)

`plan_restore(backup_id, shared_root) -> RestorePlan` lists configs and skills folders to put back, and skills in the shared root created or changed after the backup (from `git log --since` on the setup commit). `restore(plan, copy_new_to: dict[skill, list[profile]] )` moves current profile skills folders into a new backup first, then restores. The shared root is never modified. Test: setup, then an edit in the shared root, then restore: profile trees equal the original byte for byte, the shared root still has the edit, and with `copy_new_to` the edited skill appears in the chosen profile.

Phase 3 gate: end-to-end test `tests/test_setup_e2e.py` on the classic fleet: scan, propose, apply, verify (no problems), restore (byte-identical), all in one test.

---

## Phase 4: Merge, check, promote/demote, history

### Task 4.1: Merge queue (`commons/core/merge.py`)

`next_conflict(shared_root, proposal) -> MergeTask | None` (variants with paths, sizes, mtimes, unique line counts, staging dir `<shared_root>/.commons/staging/<name>/`). `validate_staged(task) -> MergeCheck` (frontmatter parses, name matches folder, referenced relative files exist, `lost_lines`: normalized variant lines present nowhere in the staged tree). `accept(task, check, force=False)` refuses when `lost_lines` is non-empty unless `force`; otherwise moves staging into the shared root, commits "merge <name>", retires profile copies, marks the conflict resolved in the stored proposal. `skip(name)`. Tests: a staged merge with all lines accepts; one missing line is refused and the line is reported; `keep` (copying one variant into staging) works with `force`.

### Task 4.2: Check (`commons/core/check.py`)

`run_check(profiles, shared_root, bundled_hashes, hub_lock) -> list[Finding]` with `Finding(severity, code, profile, skill, message)`. Codes: `plugin-off`, `create-dir-missing`, `create-dir-elsewhere`, `shadowed`, `ambiguous`, `uncommitted`, `stale-path`, `bad-frontmatter`, `name-mismatch`, `missing-ref`, `oversize` (SKILL.md > 40,000 chars), `upstream-newer` (taken-over skill whose current bundled/hub hash differs from the recorded origin). `fix_safe(findings)` commits uncommitted changes only. Render as plain text; `--quiet` prints nothing when there are no warnings. One test per code.

### Task 4.3: Promote and demote (`commons/core/moves.py` additions)

`promote(name, from_profile)`: backup, copy into shared root, commit "promote <name> from <p>", retire that profile's copy; other profiles' copies become `shadowed` findings. `demote(name, to_profile)`: copy from shared root into the profile, record `intentional_overrides` in `.commons/config.json`. Tests for both, including that check stops flagging an intentional override.

### Task 4.4: History and undo

`log(name)` = `git log --follow` on the skill folder; `undo(name, to=None)` reverts the latest commit touching only that folder, or checks the folder out at `to` and commits "undo <name> to <sha>". Refuse when the latest commit touched other folders (tell the user to pass `--to`). Tests.

---

## Phase 5: Hermes wiring

### Task 5.1: Hermes adapter (`commons/hermes_adapter.py`)

The only module importing Hermes, inside functions, each import wrapped so a missing symbol raises a clear `HermesApiChanged` error:
- `default_root()` -> `hermes_constants.get_default_hermes_root()`
- `is_deleted(path)` -> `hermes_constants.named_profile_is_deleted`
- `md5_dir_hash` -> `tools.skills_sync.dir_hash`
- `active_profile_home()` -> `hermes_constants.get_hermes_home()`
- `HermesCliWriter`
- `locate(name)` -> `tools.skills_tool.skill_search_dirs` + `locate_skill` for the active profile.
Test with monkeypatched fake modules in `sys.modules`.

### Task 5.2: Lifecycle hook (`commons/hooks.py`)

`on_skill_lifecycle(**kw)`: ignore unless `action in {"created", "edited", "patched"}`; locate the skill; if inside the shared root, `commit_paths([folder], f"{action} {name} (profile {p}, session {session_id})")`. Never raises (log at warning). `on_session_start(**kw)`: shadow check for the active profile, cached by a key of skills-dir and shared-root mtimes in `.commons/cache.json`; logs new shadows. Tests: three calls produce three commits; "loaded" produces none; an exception inside git is swallowed and logged.

### Task 5.3: CLI and slash command (`commons/cli.py`, `commons/__init__.py`)

`setup_fn(parser)` adds subcommands `setup [--root PATH] [--choose S=...] [--apply] [--yes]`, `status [--only P] [--skill S]`, `check [--fix-safe] [--quiet]`, `promote S [--from P]`, `demote S --to P`, `merge [--next|--accept S|--skip S] [--force]`, `log [S]`, `undo S [--to SHA]`, `restore [ID] [--cascade] [--yes]`. Writing commands print the plan and ask `[y/N]`; without a TTY and without `--yes` they exit 2 with "needs confirmation; rerun with --yes". `register(ctx)` registers hooks, `ctx.register_cli_command("commons", ...)` and `ctx.register_command("commons", slash_handler, args_hint="status|check")`, where the slash handler only allows `status` and `check`. Tests drive `handler_fn` with parsed args against the classic fleet (adapter monkeypatched).

### Task 5.4: Commons skill (plugin-shipped)

`commons/skills/commons/SKILL.md`, under 2,000 chars, description <= 60 chars ("Use when editing, sharing or merging skills across profiles."). Content per design. `register(ctx)` calls `ctx.register_skill("commons", Path(__file__).parent / "skills/commons/SKILL.md", description=...)` (Path, not str). Tests: frontmatter parses, size limits hold, register_skill is called with a Path. Phase 6 adopt test confirms `commons:commons` appears in a real profile's index once the plugin is enabled.

### Task 5.5: Scheduled check offer

At the end of `setup --apply`, if the user accepts: write `<default home>/scripts/commons-check.sh` (`#!/bin/sh` + `exec hermes commons check --fix-safe --quiet`), then run `hermes cron create "0 * * * *" --script commons-check.sh --name "commons hourly check" --no-agent` (confirm the exact flags with `hermes cron create --help` during the task and record them in the code). Test only the script writing and the command line built (subprocess mocked).

### Task 5.6: Validator

Run `hermes plugins validate ~/hermes-commons --install-deps`. Fix every failure and every warning about non-public imports. Gate: zero failures.

---

## Phase 6: Real-data acceptance (sandbox only)

### Task 6.1: Sandbox from this morning's backups

Script `scripts/make_sandbox.sh` (not shipped in the package) that extracts `~/.hermes/backups/skills-unify-20261120T035331Z/{default,device,pikvm,pocket}-skills.tar` into `~/.hermes/cache/scratch/commons-sandbox/` laid out as a Hermes root. Never point any command at `~/.hermes` itself in this phase.

### Task 6.2: Acceptance run

`hermes commons setup --root <sandbox>/shared-skills` with the adapter's default root overridden to the sandbox (env var `COMMONS_HERMES_ROOT`, test-only, documented). Gates:
- report shows 246 names, 53 differing, 11 contained, 42 conflicts (if it differs, explain the difference against `~/.hermes/cache/scratch/profile_audit/plan.json` before changing anything);
- after apply, identical and contained skills are byte-identical to the corresponding folders in the live `~/.hermes/shared-skills` at commit `094cf29` (read-only comparison);
- verify reports no problems; restore makes the sandbox byte-identical to the extracted tars.

### Task 6.3: Adopt test on the live fleet (read-only)

`hermes commons status` and `hermes commons setup` (no `--apply`) on Chris's live fleet: must detect the existing shared root and propose no moves; findings limited to known ones (plugin-off until Chris enables it, the 5 intentional superpowers shadows show as external losing to shared, which is not a finding). Report the output to Chris before enabling the plugin live.

---

## Phase 7: Publish (each step needs Chris's OK)

1. README: why, what, install, how it differs from Guild, disclosure, limitations (hook misses non-skill-tool edits; Windows hosts untested).
2. Create `github.com/Chrisyk/hermes-commons` (public), push, tag v0.1.0.
3. Record the ~60 s demo on the sandbox fleet.
4. Catalog PR: `plugin-catalog/commons.yaml` pinned to the full 40-char sha, `requires_hermes: ">=0.23.0"`, capabilities matching plugin.yaml, disclosure text.
5. r/hermesagent Showcase post and a comment on issue #19451, text approved by Chris first.

---

## Execution notes

- Order is strict: each phase's gate must pass before the next phase starts.
- Each task: write the failing test, run it (fails), implement, run (passes), run the full suite, commit.
- Reviews per task: spec compliance against this plan and the design doc, then code quality.
- Nothing in Phases 0-5 touches `~/.hermes`; Phase 6 only reads it; Phase 7 publishes.
