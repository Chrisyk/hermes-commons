#!/bin/sh
# Build a sandbox Hermes root from the skills backup tars of 2026-11-20 03:53 UTC,
# for the Phase 6 acceptance run. Development only: not part of the plugin package.
#
#   scripts/make_sandbox.sh [DEST] [BACKUP_DIR]
#
# DEST defaults to ~/.hermes/cache/scratch/commons-sandbox and must not exist yet.
# Layout written (the shape Hermes uses for a root with named profiles):
#   DEST/config.yaml, DEST/skills/                    default profile
#   DEST/profiles/<p>/config.yaml, .../<p>/skills/    device, pikvm, pocket
# Each tar holds one profile's skills/ folder, including .bundled_manifest and
# .hub/lock.json, so bundled and hub provenance is the real one. The tars also
# hold that profile's real config.yaml and SOUL.md: only skills/ is extracted.
# Each config.yaml is minimal and reproduces the pre-consolidation state that
# matters to commons: no skills.create_dir, no skills.external_dirs (the real
# configs then listed vendor/pstack and ~/.omh skills dirs; they are left out so
# the sandbox reads nothing outside itself), plugins.enabled empty.
# Cron stores are not reproduced (default-cron-jobs.json names real paths).
set -eu

BACKUP=${2:-"$HOME/.hermes/backups/skills-unify-20261120T035331Z"}
DEST=${1:-"$HOME/.hermes/cache/scratch/commons-sandbox"}
PROFILES="default device pikvm pocket"

die() { echo "make_sandbox: $*" >&2; exit 1; }

case "$DEST" in
  /*) ;;
  *) die "DEST must be an absolute path: $DEST" ;;
esac
case "$DEST/" in
  */../*|*/./*) die "DEST must not contain . or .. segments: $DEST" ;;
esac
HERMES="$HOME/.hermes"
case "$DEST/" in
  "$HERMES/cache/scratch/"?*) ;;
  "$HERMES"/*) die "refusing to write under $HERMES outside $HERMES/cache/scratch: $DEST" ;;
esac
if [ -e "$DEST" ] && [ -n "$(ls -A "$DEST" 2>/dev/null || echo x)" ]; then
  die "$DEST already exists and is not an empty folder; remove it first"
fi
for p in $PROFILES; do
  [ -f "$BACKUP/$p-skills.tar" ] || die "missing $BACKUP/$p-skills.tar"
  # Besides skills/ the tars hold the profile's real config.yaml and SOUL.md;
  # those are never extracted (only the skills member is). Anything else stops us.
  if tar -tf "$BACKUP/$p-skills.tar" | grep -Ev '^(skills(/|$)|config\.yaml$|SOUL\.md$)' | grep -q .; then
    die "$p-skills.tar has unexpected members outside skills/"
  fi
  if tar -tf "$BACKUP/$p-skills.tar" | grep -Eq '(^|/)\.\.(/|$)'; then
    die "$p-skills.tar has a .. member"
  fi
done

mkdir -p "$DEST"
for p in $PROFILES; do
  if [ "$p" = default ]; then home="$DEST"; else home="$DEST/profiles/$p"; fi
  mkdir -p "$home"
  tar -xpf "$BACKUP/$p-skills.tar" -C "$home" skills
  cat >"$home/config.yaml" <<EOF
# commons Phase 6 sandbox, profile $p: pre-consolidation state of 2026-11-20.
model:
  default: sandbox
skills:
  creation_nudge_interval: 15
plugins:
  disabled: []
  enabled: []
EOF
  for f in .bundled_manifest .hub/lock.json; do
    if [ -f "$home/skills/$f" ]; then
      echo "$p: skills/$f present"
    else
      echo "$p: skills/$f MISSING (its skills will be classed as own)"
    fi
  done
done
echo "sandbox ready: $DEST"
