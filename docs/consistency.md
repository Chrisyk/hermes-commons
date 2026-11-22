# Consistency: several agents writing one shared skill

commons 0.2 adds two settings that control what happens when two agents write
the same shared skill. Both are off unless you turn them on, so upgrading from
0.1 changes nothing.

| Setting | Values | Missing means |
|---|---|---|
| `lock` | `own`, `shared` | `own` |
| `guard` | `off`, `warn`, `strict` | `off` |
| `guard.<profile>` | `off`, `warn`, `strict` | the fleet `guard` |

```
hermes commons config                      # show
hermes commons config set lock shared
hermes commons config set guard warn
hermes commons config set guard.pocket off # one profile only
hermes commons config set guard.pocket unset
```

The settings live in `<hermes home>/commons/consistency.json`, outside the
shared skills folder, so neither an agent editing skills nor a `git checkout`
there can change them. Only `hermes commons config set` writes the file.

## The problem

Hermes already serializes skill writes inside one profile: `skill_manage`
holds a per-skill file lock for the whole read, change and write, and writes
through a temp file and rename. But the lock file lives in the active
profile's own `skills/.locks/` folder, not next to the skill. Two profiles
writing the same shared skill take different locks, so the later rename can
drop the earlier edit. In a test with two real Hermes processes appending 40
lines each to one skill, 4 to 7 of the 80 lines were lost per run with each
profile's own lock, and none with `lock shared`
(`tests/test_two_process_race.py`).

The second problem is a stale rewrite: an agent reads a skill, works for a
few minutes, then rewrites the whole file from its old copy and erases every
edit made since. No lock helps there, because the read and the write are
separate tool calls.

## lock

`lock shared` makes every profile's `skills/.locks` a symlink to one folder,
`<hermes home>/commons/skill-locks/shared`. Hermes' own lock then covers all
profiles. commons adds no hook and holds no lock during a turn; the kernel
frees a lock if its process dies.

- Cost: one extra open and lock per skill write. A write waits only while
  another profile is writing the same skill (a patch takes about 5 to 10 ms).
- Also on with `lock shared`: the per-edit commit hook copies the skill's files
  inside the writer's lock. If the skill has changed again by the time the
  commit runs, that version is kept in `<hermes home>/commons/overwritten/`
  and `check` reports it.
- Switching moves real `.locks` folders into
  `<hermes home>/commons/backups/consistency-<time>/` (never deleted). After
  the first switch, `.locks` stays a symlink, so later switches are one
  atomic rename. A switch refuses while a skill write is in progress.
- A write that already opened its lock file when you switch can overlap once
  with a write that starts on the new link.
- `lock own` is Hermes' default: each profile its own lock.
- `check` reports `locks-unshared` when a profile (for example one added
  later) does not use the shared folder; run `config set lock shared` again.

A first `hermes commons setup --apply` sets `lock shared` (use `--lock own`
to keep Hermes' default). An existing install keeps its setting, and
`hermes commons restore <setup id>` puts each profile's own `.locks` back.

## guard

The guard refuses a write that is based on an old read. It remembers, per
session and file, the version the session last read (or last wrote itself).

- A full rewrite (`skill_manage` patch with `content`, `write_file`,
  `remove_file`, `delete`, or the generic `write_file` tool on a file in the
  shared folder) is allowed only if the file still matches that version. A
  new file needs no read.
- A targeted patch is allowed when its `old_string` occurs exactly in the
  file. Otherwise it needs an unchanged file, because Hermes' fuzzy matching
  could paste a stale patch over another agent's new text.
- A read in the same model response as the write does not count.
- `strict` refuses with a message that tells the agent to re-read the file and
  re-apply its change. `warn` lets the write through and logs what it would
  have refused to `<hermes home>/commons/guard.log`. Try `warn` for a few days
  before `strict`.
- The guard hooks are registered only when some profile runs the guard, so at
  `off` no tool call pays for them. Turning the guard on from `off` needs a
  restart of the gateway and open chats; switching between `warn` and
  `strict`, or back to `off`, applies on the next tool call.
- Any error inside the guard lets the call through.

## Not covered

- Edits through `terminal` or `execute_code` (sed, `cat >`, git): the hooks
  cannot see them. `check` reports them as uncommitted and
  `check --fix-safe` commits them.
- Profiles without commons enabled.
- Windows is supported but only checked by CI on a Windows runner so far. Differences:
  file locks use `msvcrt.locking` (the repo lock uses a byte far into the file so
  the holder note stays readable; the cron lock uses byte 0 like Hermes); `lock shared`
  links `skills/.locks` with a directory junction, which needs no admin rights
  (it falls back to a real symlink, which needs Developer Mode or admin, if a
  junction cannot be made); file modes and executable bits do not exist, so
  `chmod` and the executable-bit checks have no effect.
- A write that lands in the few milliseconds between the guard's check and
  Hermes taking its lock. Closing that needs Hermes to key its lock by the
  skill's real folder and accept an expected hash; that is an upstream
  change.
