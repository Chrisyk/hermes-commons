# Changelog

## 0.2.1 (2026-11-21)

- Fix enabling commons in more than one profile: Hermes refused the second
  profile ("Two workspace members are both named hermes-commons"). The
  package no longer declares a build backend, so Hermes names each
  profile's copy uniquely.

## 0.2.0 (2026-11-21)

- Consistency settings, `hermes commons config`: `lock own|shared` and
  `guard off|warn|strict`, with per-profile guard levels. See
  [docs/consistency.md](docs/consistency.md).
- `lock shared` points every profile's `skills/.locks` at one folder, so
  Hermes' own per-skill lock serializes writes across profiles. The commit
  hook then also keeps any written version that is replaced before git sees
  it, in `<hermes home>/commons/overwritten/`.
- The guard refuses (strict) or logs (warn) a write based on an old read.
- A first `setup --apply` sets `lock shared`; `--lock own` keeps Hermes'
  default. `restore` of that setup puts each profile's own `.locks` back.
- New `check` findings: `locks-unshared`, `locks-mismatch`,
  `consistency-config`, `guard-override`, `overwritten`.
- `status` shows the settings, and with `lock own` how often two profiles
  edited the same skill within a minute.

Upgrading from 0.1 changes nothing: with no settings file, lock is `own` and
the guard is `off`, and commons registers the same two hooks as before.

## 0.1.0 (2026-11-20)

First release.
