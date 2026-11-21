---
name: commons
description: Use when editing, sharing or merging skills across profiles.
---
# commons: one shared skill library

Every profile reads shared skills from one folder, the shared root (its
`skills.create_dir`). Edit a shared skill in place there; each edit is
committed to git, so it can be undone.

Rules:
- Run `hermes commons check` before and after skill maintenance.
- Keep one copy: never copy a shared skill into a profile's own skills
  folder, because that copy would shadow the shared one in that profile. If a private variant is
  really wanted, run `hermes commons demote S --to P`.
- To share a profile's own skill, run `hermes commons promote S --from P`.
- Find where a skill resolves: `hermes commons status --skill S`.
- History and undo: `hermes commons log S`, `hermes commons undo S`.
- Other agents edit shared skills too. Prefer a patch whose old_string is
  copied exactly from text you just read over rewriting the whole file.
- If a write is refused with "commons: nothing was written", read the file
  again in a separate step and re-apply your change to the new text. Do not
  resend the same call.
- Consistency settings: `hermes commons config` (lock, guard). Change them
  only when the user asks.

Agent merge of a conflict (copies that differ between profiles):
1. `hermes commons merge --next` prints the variants, their unique lines
   and a staging folder (`--seed variant:P` starts it from one variant).
2. Write the merged skill into the staging folder. Keep every unique line
   of every variant; moving text verbatim into references/ is fine when
   SKILL.md gets long.
3. `hermes commons merge --accept S` checks it and lists any lost line.
   Add what is missing and run it again; it asks before landing the merge.
4. `hermes commons merge --skip S` leaves S for later.

Writing commands ask first; pass `--yes` only when the user agreed.
