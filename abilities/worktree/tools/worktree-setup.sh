#!/usr/bin/env bash
# worktree-setup.sh — ship-check per-session git worktree provisioner (v1.49.65).
#
# WHY: multiple ship-check sessions share one git working tree per target package.
# Measured incident (KB anti-shared-workspace-head-moves-during-gate-wait): a session read
# HEAD at Context, waited 3.7h at the [CF00] gate, and by then another session had committed
# to the same repo — `rev-list --count origin/<base>..HEAD` = 2, so a bare `cr` would
# have packaged a stranger's commit into this task's CR. 45.7% (112/245) of historical plan
# docs target the SAME package, so the highest-parallelism demand coincides with the highest
# collision rate.
#
# WHAT: gives each ship-check session its own worktree + branch `shipcheck/<uuid-short>`.
#
# TWO MODES (auto-detected — Phase -1 measured, see the plan doc's "Phase -1 实测结论"):
#   plain   : source repo is NOT inside a brazil workspace -> worktree at
#             <root>/<uuid-short>/<repo-basename>/
#   brazil  : source repo IS inside a brazil workspace     -> a 2.7M workspace SKELETON is
#             provisioned at <root>/<uuid-short>/<workspace-basename>/ and the worktree is
#             mounted at <that>/src/<Pkg>.
#             Required because a bare worktree fails with
#               "Directory isn't within a workspace: <path>"
#             (BrazilBuildTools workspace boundary check). With the skeleton, `brazil-build`
#             reaches a byte-identical stage to the original workspace.
#
# PATH LAYOUT (v1.49.66): every path carries BOTH a session and a repo dimension —
#   <root>/<uuid-short>/<workspace-or-repo-basename>[/src/<Pkg>]
# The repo level is load-bearing, not cosmetic: without it two repos in one session share a
# skeleton root and, because this script is idempotent, the second one silently reuses (and
# corrupts) the first one's `packageInfo` / `env` / `.brazil`. Grouping under <uuid-short>
# keeps `worktree-teardown.sh --uuid` a single reachable sweep point for the whole session.
#
# IDEMPOTENT: safe to re-run for the same --uuid (resume / retry). An existing worktree is
# reused, never recreated; an orphaned branch is re-attached rather than duplicated.
#
# READ-ONLY w.r.t. the SOURCE repo's working tree and index: this script only ever runs
# `git -C <source> worktree add` (which touches .git/worktrees/, never the source checkout)
# and copies FROM the brazil workspace root. It never stages, commits, stashes or resets.
#
# Usage:
#   worktree-setup.sh --uuid <shipcheck-uuid> --source-repo <path> [--root <dir>] [--dry-run]
#
#   Trees are created under this ability's own `state/worktrees/` by default.
#   Override with --root, or with HARNESS_WORKTREE_ROOT for a whole host.
#
# Output (last lines, machine-readable — callers parse these):
#   MODE=plain|brazil
#   WORKTREE_PATH=<abs path>       <- persist THIS as metadata_json.workspace
#   BRANCH=shipcheck/<uuid-short>
#   REUSED=true|false
#   SESSION_ROOT=<abs path>        <- <root>/<uuid-short>; teardown --uuid sweeps this
#   WS_KEY=<workspace-or-repo-basename>
#
# Exit: 0 = ok | 2 = operational error | 3 = usage/env error
set -euo pipefail

UUID=""
SOURCE_REPO=""
ROOT_BASE=""   # empty = not given on the CLI; resolved after arg parse (see below)
DRY_RUN=false

while [ $# -gt 0 ]; do
  case "$1" in
    --uuid)        UUID="${2:-}"; shift 2 ;;
    --source-repo) SOURCE_REPO="${2:-}"; shift 2 ;;
    --root)        ROOT_BASE="${2:-}"; shift 2 ;;
    --dry-run)     DRY_RUN=true; shift ;;
    -h|--help)     grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "ERR: unknown arg: $1" >&2; exit 3 ;;
  esac
done

[ -n "$UUID" ]        || { echo "ERR: --uuid is required" >&2; exit 3; }
[ -n "$SOURCE_REPO" ] || { echo "ERR: --source-repo is required" >&2; exit 3; }

# ---------------------------------------------------------------------------
# WHERE THE TREES LIVE (moved into this ability): `<this ability>/state/worktrees`.
#
# Resolved from THIS SCRIPT's own location, so it moves with the checkout and names no
# absolute path — the same reason `produced_by` in the consuming flow is relative.
#
# `state/` is the FIRST line of the engine repo's .gitignore and matches at any depth, which
# is the load-bearing part: one brazil session measured 216,384 files / 29 MB, and an
# un-ignored root would make `git add -A` in the engine repo commit a whole checkout. A guard
# in tests/ asserts the root this resolves to is ignored, because that property is invisible
# until the day someone renames the directory.
#
# Precedence: --root (CLI) > HARNESS_WORKTREE_ROOT (env) > ability-local default. The env var
# exists for a host where the checkout is read-only or on a different volume; the engine
# itself never reads it (it is this tool's knob, not an engine setting).
#
# The default REFUSES rather than guesses when it cannot see the ability next to it — invoked
# through a symlink placed elsewhere, `dirname $0` is the link's directory, and silently
# rooting trees at some unrelated `state/worktrees` is the failure you would not notice.
# ---------------------------------------------------------------------------
if [ -z "$ROOT_BASE" ]; then ROOT_BASE="${HARNESS_WORKTREE_ROOT:-}"; fi
if [ -z "$ROOT_BASE" ]; then
  _tools_dir="$(cd -P "$(dirname "$0")" && pwd)"
  _ability_dir="$(cd -P "$_tools_dir/.." && pwd)"
  [ -f "$_ability_dir/flow.yaml" ] || {
    echo "ERR: cannot locate the worktree ability from \$0=$0 —" >&2
    echo "     expected $_ability_dir/flow.yaml. Pass --root <dir> or set HARNESS_WORKTREE_ROOT." >&2
    exit 3
  }
  ROOT_BASE="$_ability_dir/state/worktrees"
fi
[ -d "$SOURCE_REPO" ] || { echo "ERR: --source-repo not a directory: $SOURCE_REPO" >&2; exit 3; }

git -C "$SOURCE_REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "ERR: not a git repo: $SOURCE_REPO" >&2; exit 3; }

# Resolve to the repo TOP level: --source-repo may point at a subdirectory (e.g. the
# ship-check skill dir inside the ~/.kiro/skills repo). A worktree can only be added for
# the whole repository, so anchor on the toplevel.
REPO_TOP="$(git -C "$SOURCE_REPO" rev-parse --show-toplevel)"

UUID_SHORT="$(printf '%s' "$UUID" | cut -c1-8)"
BRANCH="shipcheck/${UUID_SHORT}"

# SESSION_ROOT groups EVERY worktree this session provisions, across repos.
# The per-repo directory is appended below, AFTER brazil detection — the skeleton is a
# property of the workspace, so its key can only be known once the workspace is resolved.
#
# WHY the extra level (v1.49.66): the path used to be `<base>/<uuid-short>` with no repo
# dimension, so a composite plan touching two repos under one session collided — the second
# repo silently REUSED the first one's skeleton root (this script is idempotent by design),
# cross-contaminating `packageInfo` / `env` / `.brazil`. Sessions then worked around it by
# passing ad-hoc `--root` values, which put the trees outside the GC's default sweep root and
# leaked 1.85G. Keying on `<uuid-short>/<workspace-basename>` fixes both: no collision, and
# one `--uuid` teardown still reaches every tree the session created.
SESSION_ROOT="${ROOT_BASE}/${UUID_SHORT}"

# ---------------------------------------------------------------------------
# Brazil workspace detection — walk UP from the repo looking for a workspace root.
# A brazil workspace root holds BOTH `packageInfo` and `env/` (verified 2026-08-11 against
# a real brazil workspace). `.brazil/` is also present and is copied
# below, but it is NOT used as the detection signal on its own.
# ---------------------------------------------------------------------------
detect_brazil_root() {
  local d="$1"
  while [ "$d" != "/" ] && [ -n "$d" ]; do
    if [ -f "$d/packageInfo" ] && [ -d "$d/env" ]; then
      printf '%s' "$d"
      return 0
    fi
    d="$(dirname "$d")"
  done
  return 1
}

BRAZIL_ROOT=""
if BRAZIL_ROOT="$(detect_brazil_root "$REPO_TOP")"; then
  MODE="brazil"
  PKG_NAME="$(basename "$REPO_TOP")"
  # Repo dimension = the WORKSPACE basename (not the package name): the skeleton is
  # per-workspace, and `src/<Pkg>` below already carries the package dimension.
  WS_KEY="$(basename "$BRAZIL_ROOT")"
  ROOT="${SESSION_ROOT}/${WS_KEY}"
  WORKTREE_PATH="${ROOT}/src/${PKG_NAME}"
else
  MODE="plain"
  BRAZIL_ROOT=""
  PKG_NAME=""
  # No workspace to key on → use the repo's own basename.
  WS_KEY="$(basename "$REPO_TOP")"
  ROOT="${SESSION_ROOT}/${WS_KEY}"
  WORKTREE_PATH="$ROOT"
fi

echo "worktree-setup: uuid=$UUID_SHORT mode=$MODE repo=$REPO_TOP"
echo "worktree-setup: session root=$SESSION_ROOT repo key=$WS_KEY"
[ "$MODE" = "brazil" ] && echo "worktree-setup: brazil workspace root=$BRAZIL_ROOT pkg=$PKG_NAME"

if [ "$DRY_RUN" = true ]; then
  echo "worktree-setup: DRY RUN — no changes made"
  echo "MODE=$MODE"
  echo "WORKTREE_PATH=$WORKTREE_PATH"
  echo "BRANCH=$BRANCH"
  echo "SESSION_ROOT=$SESSION_ROOT"
  echo "WS_KEY=$WS_KEY"
  echo "REUSED=unknown"
  exit 0
fi

# ---------------------------------------------------------------------------
# Idempotency: is this worktree already provisioned?
# `git worktree list --porcelain` emits `worktree <path>` lines. Matched with awk (not a
# `while read` + heredoc) deliberately: a heredoc needs a temp file, and a full temp-fs makes
# it hang silently (see data/platform-quirks.md). awk also handles paths containing spaces,
# which `$2`-style field splitting would truncate.
# ---------------------------------------------------------------------------
ALREADY=false
if git -C "$REPO_TOP" worktree list --porcelain \
   | awk -v target="$WORKTREE_PATH" '
       /^worktree / { p = $0; sub(/^worktree /, "", p); if (p == target) found = 1 }
       END { exit(found ? 0 : 1) }
     '; then
  ALREADY=true
fi

if [ "$ALREADY" = true ] && [ -e "$WORKTREE_PATH/.git" ]; then
  echo "worktree-setup: reusing existing worktree (idempotent no-op)"
  echo "MODE=$MODE"
  echo "WORKTREE_PATH=$WORKTREE_PATH"
  echo "BRANCH=$BRANCH"
  echo "SESSION_ROOT=$SESSION_ROOT"
  echo "WS_KEY=$WS_KEY"
  echo "REUSED=true"
  exit 0
fi

# Stale registration (dir deleted but git still lists it) → prune just that entry.
if [ "$ALREADY" = true ]; then
  echo "worktree-setup: stale worktree registration detected → pruning"
  git -C "$REPO_TOP" worktree prune
fi

# ---------------------------------------------------------------------------
# brazil mode: provision the 2.7M workspace skeleton.
# Inventory verified by probe 2026-08-11 — `build/` (20M) is deliberately NOT copied
# (brazil creates what it needs; copying it made the skeleton 22M for no benefit).
# `.brazil/` MUST be copied rather than shared: it holds `locks/`, and a shared lock
# directory would re-serialise the very sessions this feature parallelises.
# ---------------------------------------------------------------------------
if [ "$MODE" = "brazil" ]; then
  mkdir -p "$ROOT/src"
  for item in .brazil env packageInfo release-info logs; do
    src="$BRAZIL_ROOT/$item"
    dst="$ROOT/$item"
    if [ ! -e "$src" ]; then
      echo "worktree-setup: WARN skeleton item absent in source workspace: $item"
      continue
    fi
    if [ -e "$dst" ]; then
      continue    # idempotent — already provisioned
    fi
    # cp -R preserves symlinks as symlinks (env/ holds ~53k of them).
    cp -R "$src" "$dst"
  done
  echo "worktree-setup: skeleton provisioned at $ROOT ($(du -sh "$ROOT" 2>/dev/null | cut -f1 || echo '?'))"
else
  mkdir -p "$(dirname "$ROOT")"
fi

# ---------------------------------------------------------------------------
# Create the worktree. Re-attach an existing branch instead of failing, so a session whose
# worktree directory was removed but whose branch survives can resume.
# ---------------------------------------------------------------------------
if git -C "$REPO_TOP" show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  echo "worktree-setup: branch $BRANCH exists → re-attaching worktree to it"
  git -C "$REPO_TOP" worktree add "$WORKTREE_PATH" "$BRANCH"
else
  git -C "$REPO_TOP" worktree add -b "$BRANCH" "$WORKTREE_PATH"
fi

# Post-hoc verification: the source repo must be untouched, and the worktree must be usable.
git -C "$WORKTREE_PATH" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "ERR: created worktree is not usable: $WORKTREE_PATH" >&2; exit 2; }

if [ "$MODE" = "brazil" ]; then
  # Confirm brazil recognises the skeleton. Advisory only: `brazil` may be absent (non-brazil
  # machine) and the whole feature must not hard-fail on a missing optional toolchain.
  if command -v brazil >/dev/null 2>&1; then
    if ( cd "$WORKTREE_PATH" && brazil workspace show >/dev/null 2>&1 ); then
      echo "worktree-setup: brazil workspace recognised ✅"
    else
      echo "worktree-setup: WARN brazil did not recognise the skeleton — build steps may fail"
    fi
  fi
fi

echo "MODE=$MODE"
echo "WORKTREE_PATH=$WORKTREE_PATH"
echo "BRANCH=$BRANCH"
echo "SESSION_ROOT=$SESSION_ROOT"
echo "WS_KEY=$WS_KEY"
echo "REUSED=false"
