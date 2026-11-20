# commons: one skill library for every Hermes profile

Status: draft for Chris's final approval, 2026-11-20 (approved; Q1 revised to plugin-shipped skill, Q2=A). Working name `commons` (free in the plugin catalog).

## Intent

People who run several Hermes profiles end up with a copy of every skill in every profile. The copies drift apart, one profile quietly becomes a fork, and the skill index repeats the same text in every prompt. Chris's own fleet on 2026-11-20: 4 profiles, 246 skill names, 53 with different versions in different profiles, one profile (device) holding weeks of edits the others never got, skill index 50-60% of every prompt.

Hermes already lets every profile point at one folder (`skills.create_dir`), but nothing keeps that folder healthy: a profile's own copy silently hides the shared one, edits have no history, the built-in curator skips the folder, and nothing helps you get from "N drifting copies" to "one library". Upstream issue NousResearch/hermes-agent#19451 asks for this and is still open with no decision.

commons is a Hermes plugin plus a small skill that (1) migrates a fleet to one live shared folder through a setup that shows the duplicates and proposes a merge, and (2) keeps that folder healthy afterwards.

It is also a public portfolio project: it gets published on GitHub, submitted to the Hermes plugin catalog, and announced on r/hermesagent.

## Goals

1. `hermes commons setup`: scan every profile, report duplicates and drift, propose a merge, change nothing until the user approves, then apply it with backups and verify.
2. One live folder read by every profile. An edit in one profile is visible to all others on their next skill lookup. No copies, no sync job.
3. Every skill edit an agent makes through Hermes is committed to git as its own commit, so any single edit can be undone.
4. Detect and report drift: a profile's own copy hiding a shared skill, two copies with different content, cron jobs or skills pointing at old skill paths, broken frontmatter, missing referenced files.
5. Move skills into and out of the shared folder safely (`promote`, `demote`).
6. Fully reversible: `hermes commons restore` puts every profile back to its pre-setup state.

## Non-goals

- Syncing across machines. The library is a git repo, so users can push it anywhere themselves; commons does not run git push/pull. (Guild and the skillfleet tools cover cross-machine sync.)
- Team review workflows. That is Guild's job.
- Managing bundled or hub-installed skills. Those stay per profile; `hermes update` and the hub own them.
- Reducing the skill index by hiding skills. Hermes deliberately never hides index rows.

## Anti-goals (would be a failure even if it works)

- Losing any skill content. Nothing is ever deleted; replaced copies move to a backups folder.
- Touching anything without an explicit approval in setup.
- Adding tool schemas or per-turn text to the prompt. The only prompt cost is the one index row of the plugin's `commons:commons` skill (about 80 chars), and only in profiles where commons is enabled.
- Patching Hermes internals or importing undocumented core modules (catalog rule 9; validator warnings).
- Network calls of any kind.

## Constraints

- Hermes >= 0.23.0 (`requires_hermes: ">=0.23.0"` in plugin.yaml and the catalog entry). Developed against v0.23.1 (c9c87fc, 2026-11-13).
- Public plugin API only: `ctx.register_hook`, `ctx.register_cli_command`, `ctx.register_command`, `hermes_constants` (profile roots), `hermes_cli.config`, `tools.skills_sync.dir_hash`, `tools.skill_usage.read_skill_name`, `tools.skills_tool.find_all_skills/skill_search_dirs/locate_skill` (all listed in website/docs/developer-guide/plugins/public-api.md).
- Hooks used: `on_skill_lifecycle` (fires on created/edited/patched, gives skill_name, action, session_id; not the path) and `on_session_start`.
- The hook fires only for edits made through Hermes' skill tool. Edits with write_file, patch, the shell or a text editor do not fire it; `check` and the scheduled check pick those up in a batch commit.
- Skills always live on the Hermes host, even for profiles whose terminal runs over SSH, so commons never needs the remote machines.
- Must not hang unattended runs (cron, gateway): anything needing approval fails cleanly with a message when there is no TTY.
- Catalog disclosure (rule 13): reads every profile's config.yaml, skills folders and cron/jobs.json; runs local git; writes to the shared folder and, during setup/restore only, to each profile's config.yaml and skills folder.
- Linux and macOS first. Windows Hermes hosts are not tested in v1.
- No em-dashes in code, comments, commits or PR text (Chris's style).

## Approaches considered

1. **Plugin + small skill (chosen).** Uses Hermes' existing `skills.create_dir` so every profile reads one folder live. Only a plugin can receive the skill-edit hook (per-edit commits) and add `hermes commons` subcommands. The skill only carries guidance the agent must read. Limited to Hermes, which is the point: the Hermes-specific precedence and drift checks are what nothing else does.
2. Standalone cross-agent tool (symlink one library into every agent's folder). Competes with PhoenixSmith/skillfleet and onmax/skillfleet and loses the Hermes-specific checks. Rejected.
3. Change Hermes itself (implement #19451). Strongest if merged, but the issue has no maintainer decision and could sit for months. Rejected for now; commons can later be offered as the working reference.

Guild (already in the catalog) gives each profile its own git clone of a team library, synced every 15 minutes, loaded at the lowest precedence so local copies still win, with review before merge and team skills read-only. commons is for one person on one Hermes install: one live folder, no clones or sync delay, edits in place, plus migration and drift checks. Different enough for its own listing (catalog rule 16); the README and PR say so explicitly.

### Merge strategy for skills that differ between profiles (Chris picked C)

- A. User picks one copy per skill; the other is backed up.
- B. The agent merges each conflict, keeping both copies' unique content; the user reviews the git diff.
- **C. Both (chosen).** Copies whose content is contained in another copy resolve automatically (keep the superset). For the rest, the user chooses per skill between picking a copy and an agent merge.

Measured on Chris's fleet: containment resolved 11 of 53; 42 needed a real merge. So the agent-merge path is the main path, not an edge case, and must be solid.

## Design

### Terms

- **Shared root**: the folder every profile's `skills.create_dir` points at. Default `<default HERMES_HOME>/shared-skills`. A git repo.
- **Source** of a skill in a profile: `project`, `local` (profile's own folder), `shared`, `external` (external_dirs), plus provenance labels `bundled`, `hub`, `symlink`.
- **Shadowed**: a shared skill hidden in some profile by a higher-precedence copy (project or local) with the same name. Hermes precedence: project > local > create_dir > external_dirs.
- **Profiles**: the default root (`hermes_constants`) plus every `profiles/<name>/` with a config.yaml.

### Commands

`hermes commons ...` (CLI) and `/commons ...` (slash command, read-only subset: status, check):

| Command | Does | Writes? |
|---|---|---|
| `setup` | Scan, report, propose, then on approval apply and verify. Re-runnable: on a fleet already set up it adopts the existing shared root and only proposes what is left. | Only after approval |
| `status [--profile P] [--skill S]` | Per profile, every skill with its source; totals; prompt-index size per profile. | No |
| `check [--fix-safe] [--quiet]` | Drift report (see below). `--fix-safe` commits uncommitted changes in the shared root and nothing else. `--quiet` prints only when something is wrong (for cron). | Only with --fix-safe |
| `promote S [--from P]` | Move a profile's own skill into the shared root; other profiles' copies of S become shadow findings for the user to resolve. Backup first. | Yes, after confirm |
| `demote S --to P` | Copy a shared skill into one profile's own folder (an intentional private override) and record it as intentional so `check` stops flagging it. | Yes, after confirm |
| `merge [--next \| --accept S \| --skip S]` | Agent-assisted conflict merge (below). | `--accept` only |
| `log [S]`, `undo S [--to COMMIT]` | Git history of a skill; revert one edit or roll a skill back. | undo: yes |
| `restore [BACKUP_ID]` | Put every profile's config.yaml and skills folder back as they were before a setup/promote/demote. | Yes, after confirm |

All writing commands show what they will do and ask; `--yes` skips the question; with no TTY and no `--yes` they exit non-zero with a message.

### Setup flow

1. **Scan.** For each profile: read config.yaml (skills.create_dir, skills.external_dirs, disabled), walk its local skills folder, classify each skill by provenance (bundled manifest `.bundled_manifest`, hub lock under `.hub/`, symlink, own), and hash each skill folder with `dir_hash`.
2. **Group** by skill name across profiles into: identical everywhere; contained (one copy's non-trivial lines and files are a superset of every other copy's); conflicting; stays-local (pristine bundled, pristine hub, symlink); in-profile duplicates (two folders with one name in one profile). Customized bundled/hub copies join the identical/contained/conflicting groups and are marked "taken over" in the proposal.
3. **Report** counts, the prompt-index text that is duplicated, and the profile that is furthest ahead (most newest copies).
4. **Propose**: shared root location; which skills move there; for each contained group which copy is kept; for each conflict a default action (pick newest is never the default; the default is "agent merge"); config changes per profile; cron jobs/skills whose paths will need rewriting.
5. **Review**: the user can open the proposal (written to `<shared root>/.commons/proposal-<ts>.md` and `.json`), change per-skill choices with `setup --choose S=keep:P|merge`, then `apply` or `cancel`.
6. **Apply**: back up every profile (config.yaml plus the skills folder) to `<shared root>/.commons/backups/<ts>/`; `git init` the shared root if needed; copy chosen skills in; commit "commons setup <ts>"; set `skills.create_dir` on every profile; move each profile's now-redundant local copies into the backup (never delete); rewrite old skill paths in cron prompts and skills only where the new file exists, listing each rewrite.
7. **Verify**: for every profile, recompute where each name resolves; expect no shadowing, no same-tier ambiguity, every previously available skill still available. On failure, offer `restore` immediately.
8. Conflicts chosen as "merge" stay out of the shared root, keep working from each profile's own copy, and are listed as pending for `merge`.

### Agent merge

The agent does the merging inside a normal chat, steered by the commons skill; commons adds no agent tools.

- `hermes commons merge --next` prints the next pending conflict: the variant paths, their sizes and dates, per-variant unique lines, and a target folder in the shared root staging area.
- The agent writes the merged skill into the staging folder, keeping every variant's unique content (verbatim moves into references/ are allowed when SKILL.md would get too long).
- `hermes commons merge --accept S` validates the result (frontmatter parses, name matches, referenced files exist, and it reports any variant line that appears nowhere in the merged result), shows the diff against each variant, and on confirm moves it into the shared root, commits it, and moves the profile copies to the backup.
- The user can also do the same without an agent: copy one variant into staging and accept (this is option A).
- Several conflicts can be merged in parallel by subagents, since each touches only its own staging folder.

### Keeping it healthy

- **Per-edit commits.** `on_skill_lifecycle` handler: resolve the skill name to a folder with `locate_skill`; if it is inside the shared root, `git add` that folder and commit `"<action> <skill> (profile <p>, session <id>)"`. A file lock under `.commons/` serializes git operations across profiles and processes. Failures are logged, never raised into the agent turn.
- **Session start.** `on_session_start` runs a cheap shadow check for the active profile only (cached by folder mtimes). New findings are logged and shown by `status`/`check`. Decision: commons injects no prompt text; the commons skill tells the agent to run `hermes commons check` before editing a skill that might be shadowed.
- **check** reports: profiles where the commons plugin is not enabled; taken-over bundled/hub skills whose source has a newer version; shadowed shared skills (except ones recorded as intentional by demote); profiles whose create_dir is missing or points elsewhere; uncommitted changes in the shared root; stale skill paths in cron jobs and skills; frontmatter that does not parse or name/folder mismatches; missing referenced files; SKILL.md over a size threshold.
- **Scheduled check.** At the end of setup, offer to create a script-only cron job (no model call) running `hermes commons check --fix-safe --quiet` hourly. It is created through `hermes cron create`, and only if the user accepts.

### The commons skill

Short (well under 2k chars), description 60 chars or less. Says: shared skills live in the shared root and are edited in place; never copy a shared skill into a profile's folder (use `demote` if a private override is really wanted); use `promote` to share a profile skill; run `check` before and after skill maintenance; how to run an agent merge. Shipped inside the plugin (`commons/skills/commons/SKILL.md`) and registered with `ctx.register_skill`. Hermes lists registered plugin skills in `<available_skills>` under a `plugin` category as `commons:commons`, only in profiles where the plugin is enabled (agent/prompt_builder.py `_plugin_skill_prompt_rows`, verified 2026-11-20). It is read-only to agents and updates with the plugin, so setup writes nothing for it and `check` does not watch it.

### State

`<shared root>/.commons/`: `config.json` (profiles included, intentional overrides), `backups/<ts>/`, `proposal-*.{md,json}`, `staging/`, `lock`. `.commons/backups` and `staging` are git-ignored.

## Testing

- pytest with synthetic Hermes roots in tmp dirs (default plus 3 profiles) covering identical, contained, conflicting, bundled, hub, symlink and in-profile duplicate cases; setup apply then restore must reproduce the original trees byte for byte.
- Hook tests: simulated `on_skill_lifecycle` calls produce exactly one commit per edit; concurrent calls from two processes do not corrupt the repo.
- `hermes plugins validate <repo> --install-deps` passes with no failures.
- End-to-end on a sandbox copy of Chris's real fleet from this morning's backup tars (`~/.hermes/backups/skills-unify-*`), never the live one: setup must report the same 246/53 picture and the result must match what the manual consolidation produced. Chris's skills are never committed to the public repo as fixtures.
- Adopt test on Chris's live fleet (already consolidated): `setup` must detect the existing shared root and propose no moves; `status` and `check` must be clean except known findings.

## Packaging and launch

- Repo `github.com/Chrisyk/hermes-commons`, MIT license, README with why / what / install / how it differs from Guild / disclosure.
- Catalog PR: one file `plugin-catalog/commons.yaml`, pinned to a full commit SHA, category `tools`, capabilities matching what is registered (hooks on_skill_lifecycle and on_session_start, no tools, no env).
- Demo video, about 60 seconds: setup on the sandbox fleet showing the duplicate report, the proposal, apply, and `status` catching a shadow.
- r/hermesagent post (Showcase) and a comment on issue #19451, both only after Chris approves the text.

## Decisions after the builder check (2026-11-20)

Verified against Hermes v0.23.1 source; marked as Shelly's call unless noted.

1. The commons skill. First decision (Q1=A) was to write a normal skill into the shared root, based on plugins/index.md saying plugin skills are not indexed. The source says otherwise: `_plugin_skill_prompt_rows` (agent/prompt_builder.py ~1196) lists every skill an ENABLED plugin registers, under category `plugin`, as `commons:commons`. Chris switched (2026-11-20) to shipping it in the plugin via `ctx.register_skill`: present only where commons is enabled, read-only to agents, updated with the plugin, no setup step and nothing for `check` to watch. The docs page is stale; mention it upstream when submitting.
2. Third-party plugins only run in profiles that list them in `plugins.enabled`, and the hook fires only in the profile that made the edit. Setup's proposal includes enabling commons in every profile (shown as its own line, approved with the rest); restore removes it again; `check` reports any profile where it is not enabled. Disclosure adds "writes plugins.enabled in each profile's config.yaml during setup/restore". (Shelly's call.)
3. Restore after later work: restore puts back every profile's config.yaml and local skills folder from the backup and leaves the shared root untouched on disk. Before confirming, it lists skills created or changed in the shared root after setup and offers to copy each into the profiles that had it (or all profiles). Nothing in the shared root is deleted. (Shelly's call.)
4. End-to-end acceptance on the sandbox fleet: the setup report must match the manual audit (246 names, 53 differing; 11 auto-resolved by containment, 42 conflicts), and identical plus contained groups must land in the shared root byte-identical to the manual consolidation. Agent merges are not compared to the manual merges; they are checked by the `merge --accept` validation (no variant line lost without being listed). (Shelly's call.)
5. Customized bundled or hub skills (a copy that differs from its origin hash in `.bundled_manifest` or the hub lock). Chris chose: treat them as the user's own and move them to the shared root, grouped and merged like any other skill. Hermes' sync then sees the local copies as user-deleted and stops updating them, so commons records each taken-over skill with its origin hash in `.commons/config.json`, and `check` reports when the bundled or hub source has a newer version than the one taken over (comparing the current bundled tree / hub lock against the recorded hash). Pristine bundled and hub skills still stay per profile, except that once a name is taken over (a move, a merge accept or a promote), every pristine copy of that name in a profile that reads the shared root is retired into the backup too and recorded in `taken_over`: left in place it would shadow the shared copy (local wins). A name whose copies are all pristine stays local.
6. The hourly check lives in the default profile's cron, as a script-only job whose script commons writes to `<default HERMES_HOME>/scripts/commons-check.sh`, only if the user accepts it at the end of setup. Disclosed in the README and PR. (Shelly's call.)
7. Deletes through `skill_manage` do not fire `on_skill_lifecycle`, and write_file/remove_file arrive as "edited". Deletes are committed by `check --fix-safe` (the hourly job). (Shelly's call.)

## Left to the builder

- Exact output formatting and wording of reports.
- The "trivial line" rules used for containment (start from the rules in the manual consolidation's dominance.py).
- Size threshold for the SKILL.md warning.
- Internal module layout and test fixture structure.
