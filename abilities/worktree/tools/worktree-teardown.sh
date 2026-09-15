#!/usr/bin/env bash
# worktree-teardown.sh — ship-check per-session worktree teardown + orphan GC (v1.49.65).
#
# Companion to worktree-setup.sh. Called from the Cleanup Layer (layers/7-cleanup.md).
#
# POLICY (plan 待确认 #3 / #4):
#   - A worktree whose CR is NOT yet merged is KEPT. A live CR may still need revision or a
#     conflict-rebase, and both need the original working tree. Teardown is therefore opt-in
#     per session (--uuid) and gated on an explicit --merged / --force.
#   - Orphan GC (--gc) sweeps `shipcheck/*` branches that are already merged into the base
#     branch AND have no active ship-check session, then sweeps leftover DIRECTORY residue
#     that no longer has a branch at all (pass 2). No cron job — it piggybacks on Cleanup.
#
# DESTRUCTIVE SCOPE — read carefully:
#   Removes only (a) worktrees registered under the worktrees root and (b) `shipcheck/*`
#   branches. It NEVER touches the source repo's checkout, index, stash, or any branch
#   outside the `shipcheck/` namespace. A worktree with uncommitted changes is NOT removed
#   unless --force is given (git itself refuses; we surface that rather than forcing).
#   Every `rm -rf` goes through `safe_to_rm`, which requires BOTH "under a recognised
#   worktrees root" AND "path scoped to the session being torn down".
#
# LAYOUT-AGNOSTIC (v1.49.66): worktrees are found by asking git + walking the session dir,
#   never by matching one hard-coded path shape. Handles the current layout
#   (`<base>/<short>/<ws>[/src/<Pkg>]`) and the three legacy ones left on disk by the
#   pre-v1.49.66 collision workarounds (`<base>/<short>/src/<Pkg>`, a parallel root such as
#   `<base>-fe/...`, and an extra prefix level such as `<base>/bff/...`).
#
#   --uuid removes EVERY worktree the session created — a composite plan spanning two repos
#   is one sweep, and each tree is removed via its own owning repo (recovered from the
#   worktree's `.git` pointer file), so the caller need not name them all.
#
# Usage:
#   worktree-teardown.sh --uuid <uuid> --source-repo <path> [--merged] [--force] [--dry-run]
#   worktree-teardown.sh --gc --source-repo <path> [--base <branch>] [--dry-run]
#
#   Root resolution is IDENTICAL to worktree-setup.sh (ability-local by default), so a
#   teardown reaches what a setup created without either side naming a path.
#
# Exit: 0 = ok (incl. "kept by policy") | 2 = operational error | 3 = usage/env error
set -euo pipefail

UUID=""
SOURCE_REPO=""
ROOT_BASE=""   # empty = not given on the CLI; resolved after arg parse (see below)
BASE_BRANCH="mainline"
MERGED=false
FORCE=false
DRY_RUN=false
DO_GC=false
RMED_DIRS=""   # newline-separated dirs already rm -rf'd this run (dedup)

while [ $# -gt 0 ]; do
  case "$1" in
    --uuid)        UUID="${2:-}"; shift 2 ;;
    --source-repo) SOURCE_REPO="${2:-}"; shift 2 ;;
    --root)        ROOT_BASE="${2:-}"; shift 2 ;;
    --base)        BASE_BRANCH="${2:-}"; shift 2 ;;
    --merged)      MERGED=true; shift ;;
    --force)       FORCE=true; shift ;;
    --gc)          DO_GC=true; shift ;;
    --dry-run)     DRY_RUN=true; shift ;;
    -h|--help)     grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "ERR: unknown arg: $1" >&2; exit 3 ;;
  esac
done

[ -n "$SOURCE_REPO" ] || { echo "ERR: --source-repo is required" >&2; exit 3; }
[ -d "$SOURCE_REPO" ] || { echo "ERR: --source-repo not a directory: $SOURCE_REPO" >&2; exit 3; }
git -C "$SOURCE_REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "ERR: not a git repo: $SOURCE_REPO" >&2; exit 3; }
REPO_TOP="$(git -C "$SOURCE_REPO" rev-parse --show-toplevel)"

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

if [ "$DO_GC" = false ] && [ -z "$UUID" ]; then
  echo "ERR: either --uuid (single session) or --gc (orphan sweep) is required" >&2
  exit 3
fi

run() { # echo-and-run, honouring --dry-run
  if [ "$DRY_RUN" = true ]; then
    echo "  DRY: $*"
  else
    "$@"
  fi
}

# Is a shipcheck branch already merged into the base branch?
# Prefers the remote base (origin/<base>) and falls back to the local one, so this also works
# in a repo with no remote (delivery=local-git, e.g. ~/.kiro/skills).
resolve_base_ref() {
  if git -C "$REPO_TOP" rev-parse --verify -q "origin/${BASE_BRANCH}" >/dev/null; then
    printf 'origin/%s' "$BASE_BRANCH"
  elif git -C "$REPO_TOP" rev-parse --verify -q "$BASE_BRANCH" >/dev/null; then
    printf '%s' "$BASE_BRANCH"
  else
    # Fall back to the repo's OWN default branch (v1.49.66). Without this, GC silently
    # skipped every repo not using the `mainline` default — `~/.kiro/skills` is on `main`, so
    # the sweep that was supposed to reclaim its worktrees never even reached the branch loop.
    local head
    head="$(git -C "$REPO_TOP" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
    case "$head" in
      ""|shipcheck/*) return 1 ;;   # detached, or we are standing IN a shipcheck worktree
      *) printf '%s' "$head" ;;
    esac
  fi
}

remove_one() { # $1 = worktree path, $2 = branch, $3 = skeleton root ("" if none), $4 = owner repo
  local wt="$1" br="$2" skel="$3" owner="${4:-$REPO_TOP}"
  if [ -e "$wt" ]; then
    if [ "$FORCE" = true ]; then
      run git -C "$owner" worktree remove "$wt" --force
    else
      # Without --force git refuses when the worktree is dirty. Surface that instead of
      # destroying work: an unexpectedly dirty worktree is a signal, not an obstacle.
      if ! run git -C "$owner" worktree remove "$wt"; then
        echo "  ⚠️ worktree not removed (dirty or locked): $wt"
        echo "     inspect it, then re-run with --force if the contents are truly disposable"
        return 1
      fi
    fi
  else
    run git -C "$owner" worktree prune
  fi
  if git -C "$owner" show-ref --verify --quiet "refs/heads/${br}"; then
    run git -C "$owner" branch -D "$br"
  fi
  # Skeleton (brazil mode) — guarded by safe_to_rm, see its comment for the two predicates.
  if [ -n "$skel" ] && [ -d "$skel" ]; then
    if safe_to_rm "$skel" "${br#shipcheck/}"; then
      run rm -rf "$skel"
      # Legacy layout A has skeleton == session root, so the caller's session-root cleanup
      # would target the very same dir. Record it to keep teardown output honest (one rm per
      # dir) rather than emitting a duplicate destructive line.
      RMED_DIRS="${RMED_DIRS}${skel}
"
    else
      echo "  ⚠️ refusing to rm (outside a worktrees root, or not scoped to this session): $skel"
    fi
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Layout-agnostic discovery (v1.49.66).
#
# Paths are NOT guessed from a single hard-coded shape any more. Before v1.49.66 setup used
# `<base>/<uuid-short>` with no repo dimension, sessions worked around the resulting
# collisions with ad-hoc `--root` values, and four different layouts ended up on disk:
#   new  <base>/<short>/<ws>/src/<Pkg>   (and plain: <base>/<short>/<ws>)
#   A    <base>/<short>/src/<Pkg>        (legacy: no repo dimension)
#   B    <base>-fe/<short>/src/<Pkg>     (legacy: parallel root, outside the default base)
#   C    <base>/bff/<short>/src/<Pkg>    (legacy: extra prefix level)
# A path-shape matcher would have to enumerate all four forever. Instead we ask git, which
# knows every worktree it created regardless of where it sits, and additionally walk the
# session dir to catch trees belonging to OTHER repos (git only reports its own).
#
# MIGRATION-PERIOD CODE: the filesystem walk's legacy-A/plain branches exist only to clean up
# trees provisioned before v1.49.66. Once no session holds a pre-v1.49.66 layout they can go.
# ---------------------------------------------------------------------------

# Physical (symlink-resolved) form of a path. Needed because the two discovery sources
# disagree otherwise: `git worktree list` reports RESOLVED paths, while our filesystem walk
# reports the paths as written. On macOS `/tmp` is a symlink to `/private/tmp`, so the same
# worktree surfaced twice ("/tmp/..." and "/private/tmp/...") — `sort -u` cannot dedup those,
# and the un-canonical variant then failed the `safe_to_rm` prefix check against ROOT_BASE.
# Falls back to the input when the path does not exist (dry-run over pruned trees).
canon() {
  local p="$1"
  if [ -d "$p" ]; then ( cd "$p" 2>/dev/null && pwd -P ) && return 0; fi
  local d b
  d="$(dirname "$p")"; b="$(basename "$p")"
  if [ -d "$d" ]; then printf '%s/%s' "$( cd "$d" && pwd -P )" "$b"; else printf '%s' "$p"; fi
}

# Compare everything in physical-path space (see canon()).
ROOT_BASE="$(canon "$ROOT_BASE")"
# Parallel legacy roots (`worktrees-fe`) are SIBLINGS of the base, so the safety predicate is
# expressed relative to the base rather than by hard-coding a `.../ship-check/state/` infix —
# root-relative keeps it correct under any --root, and testable outside the real tree.
ROOT_PARENT="$(dirname "$ROOT_BASE")"
ROOT_LEAF="$(basename "$ROOT_BASE")"

# Owner repo of a worktree: read the `.git` POINTER FILE it contains.
# A worktree's `.git` is a file (`gitdir: <repo>/.git/worktrees/<name>`), not a directory —
# so the owning repo is recoverable from the worktree alone. This is what lets one --uuid
# invocation clean trees from SEVERAL repos without the caller naming them all.
owner_repo_of() {
  local wt="$1" gd
  [ -f "$wt/.git" ] || return 1
  gd="$(sed -n 's/^gitdir: *//p' "$wt/.git" | head -1)"
  [ -n "$gd" ] || return 1
  case "$gd" in
    */.git/worktrees/*) printf '%s' "${gd%/.git/worktrees/*}" ;;
    *) return 1 ;;
  esac
}

# Guard for `rm -rf`. TWO predicates, both required:
#   1. the path lives under the worktrees base OR a SIBLING parallel root (`<base>-fe`, the
#      legacy-B shape) — and strictly deeper than that root, never the root itself; and
#   2. the path is scoped to the session/branch being torn down (its name contains the short).
# (2) is new and is what stops a stray shared dir — e.g. the observed `worktrees/bff/main/` —
# from being swept while tearing down an unrelated session.
safe_to_rm() {
  local d="$1" short="$2"
  [ -n "$d" ] && [ -n "$short" ] || return 1
  [ "$d" != "/" ] || return 1
  case "$d" in
    "$ROOT_BASE") return 1 ;;                        # never the root itself
    "$ROOT_PARENT/$ROOT_LEAF"*/*) : ;;               # base + legacy parallel/prefixed roots
    *) return 1 ;;
  esac
  case "$d" in *"$short"*) return 0 ;; esac
  return 1
}

already_rmed() {
  case "
${RMED_DIRS}" in *"
$1
"*) return 0 ;; esac
  return 1
}

# Emit every worktree path belonging to shipcheck/<short>, one per line, deduped.
discover_worktrees() {
  local short="$1"
  local sess="${ROOT_BASE}/${short}"
  local cand p
  {
    # (a) git is authoritative for the repo we were pointed at — finds it at ANY root,
    #     including the legacy B/C ad-hoc roots that live outside ROOT_BASE.
    git -C "$REPO_TOP" worktree list --porcelain 2>/dev/null | awk -v br="refs/heads/shipcheck/${short}" '
      /^worktree /   { p = $0; sub(/^worktree /, "", p) }
      /^branch /     { b = $0; sub(/^branch /, "", b); if (b == br && p != "") print p }
    '
    # (b) filesystem walk of the session dir — catches trees owned by OTHER repos, which
    #     git -C "$REPO_TOP" cannot see. Covers the new layout and legacy A/plain.
    if [ -d "$sess" ]; then
      [ -e "$sess/.git" ] && printf '%s\n' "$sess"                    # legacy plain
      for p in "$sess"/src/*; do                                      # legacy A
        [ -e "$p/.git" ] && printf '%s\n' "$p"
      done
      for cand in "$sess"/*; do                                       # new: <short>/<ws>/...
        [ -d "$cand" ] || continue
        [ "$(basename "$cand")" = "src" ] && continue                 # handled above
        if [ -e "$cand/.git" ]; then
          printf '%s\n' "$cand"                                       # new plain
          continue
        fi
        for p in "$cand"/src/*; do                                    # new brazil
          [ -e "$p/.git" ] && printf '%s\n' "$p"
        done
      done
    fi
  } 2>/dev/null | while IFS= read -r line; do
    [ -n "$line" ] && canon "$line"
  done | sort -u
}

# Skeleton dir for a worktree, or "" when there is none (plain mode).
# brazil layouts all end in `<skeleton>/src/<Pkg>`, so the skeleton is two levels up.
skeleton_of() {
  local wt="$1" parent
  parent="$(dirname "$wt")"
  if [ "$(basename "$parent")" = "src" ]; then
    dirname "$parent"
  else
    printf ''
  fi
}


# ---------------------------------------------------------------------------
# Mode A: single session teardown
# ---------------------------------------------------------------------------
if [ -n "$UUID" ]; then
  UUID_SHORT="$(printf '%s' "$UUID" | cut -c1-8)"
  BRANCH="shipcheck/${UUID_SHORT}"
  ROOT="${ROOT_BASE}/${UUID_SHORT}"

  # Discover FIRST, then decide there is nothing to do. A legacy B/C session has no dir at
  # $ROOT at all (its tree sits under an ad-hoc root), so the old `[ ! -d "$ROOT" ]` early
  # exit reported "nothing to do" while leaking the tree.
  WTS="$(discover_worktrees "$UUID_SHORT")"

  if [ -z "$WTS" ] && [ ! -d "$ROOT" ]; then
    echo "worktree-teardown: nothing to do — no worktree found for $UUID_SHORT"
    exit 0
  fi

  if [ "$MERGED" = false ] && [ "$FORCE" = false ]; then
    echo "worktree-teardown: KEEPING worktree(s) for $UUID_SHORT (policy: CR not reported merged)"
    echo "  A live CR may still need a revision or a conflict-rebase, both of which need this tree."
    echo "  Re-run with --merged once the CR is merged, or --force to remove regardless."
    if [ -n "$WTS" ]; then
      OLD_IFS="$IFS"; IFS='
'
      for wt in $WTS; do echo "  would remove: $wt"; done
      IFS="$OLD_IFS"
    fi
    exit 0
  fi

  # One session may hold SEVERAL worktrees (composite plan across repos). Remove every one,
  # each via its OWN owning repo, and only drop the shared session dir once all succeeded.
  #
  # Split on NEWLINE only (no heredoc, no pipe): a heredoc needs a temp file and hangs
  # silently on a full temp-fs (data/platform-quirks.md), and a pipe would run the loop in a
  # subshell so FAILED/COUNT would not survive it. Newline-only IFS also keeps paths
  # containing spaces intact.
  FAILED=0
  COUNT=0
  OLD_IFS="$IFS"; IFS='
'
  for wt in $WTS; do
    IFS="$OLD_IFS"
    [ -n "$wt" ] || continue
    COUNT=$((COUNT + 1))
    owner="$(owner_repo_of "$wt" || true)"
    [ -n "$owner" ] || owner="$REPO_TOP"
    skel="$(skeleton_of "$wt")"
    echo "worktree-teardown: removing worktree=$wt branch=$BRANCH owner=$owner skeleton=${skel:-<none>}"
    remove_one "$wt" "$BRANCH" "$skel" "$owner" || FAILED=$((FAILED + 1))
    IFS='
'
  done
  IFS="$OLD_IFS"

  if [ "$COUNT" = 0 ]; then
    echo "worktree-teardown: no registered worktree under $ROOT — cleaning residue only"
  fi

  # Drop the now-empty session dir. Only when every removal succeeded: a surviving dirty tree
  # must keep its parent, or the `rm -rf` would destroy exactly the work git refused to drop.
  ROOT="$(canon "$ROOT")"
  if [ "$FAILED" = 0 ] && [ -d "$ROOT" ] && ! already_rmed "$ROOT"; then
    if safe_to_rm "$ROOT" "$UUID_SHORT"; then
      run rm -rf "$ROOT"
    else
      echo "  ⚠️ refusing to rm session root: $ROOT"
    fi
  fi

  if [ "$FAILED" = 0 ]; then
    echo "worktree-teardown: done — removed=$COUNT"
  else
    echo "worktree-teardown: $FAILED of $COUNT worktree(s) kept (see warnings above)"
    exit 2
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# Mode B: orphan GC — merged `shipcheck/*` branches with no active session
# ---------------------------------------------------------------------------
BASE_REF="$(resolve_base_ref)" || {
  echo "worktree-teardown: --gc skipped — base branch '$BASE_BRANCH' not found (no remote, no local)"
  exit 0
}
echo "worktree-teardown: GC sweep against $BASE_REF"

ACTIVE_SHORTS=""
SM="$HOME/.kiro/skills/ship-check/scripts/state_machine.py"
if [ -f "$SM" ]; then
  # Active sessions must never be GC'd. Degrade to "GC nothing" if the state DB is
  # unavailable (locked / missing) — a conservative default beats deleting a live tree.
  #
  # ⚠️ v1.49.66 BUG FIX: this used to call `status --format json`, which `status` does not
  # accept (it takes `[--all] [session_id]`). argparse exited 2, the `if` failed, and GC
  # aborted on EVERY invocation — so the sweep had never once run. That silent abort is a
  # direct cause of the measured leak: nothing was ever reachable for collection.
  if ACTIVE_TXT="$(python3 "$SM" status --all 2>/dev/null)"; then
    # `status --all` lists closed sessions too. A session counts as active unless its layer
    # is `closed`; `cleanup` deliberately counts as ACTIVE (it may still teardown its own
    # tree, and keeping garbage is far cheaper than deleting a live worktree).
    ACTIVE_SHORTS="$(printf '%s\n' "$ACTIVE_TXT" | awk '
      /session_id=/ {
        if ($0 ~ /current_layer=closed/) next
        s = $0
        sub(/.*session_id=/, "", s)
        sub(/[^0-9a-fA-F-].*/, "", s)
        print substr(s, 1, 8)
      }' | sort -u)"
  else
    echo "  ⚠️ state DB unreadable → GC aborted (refusing to delete without the active list)"
    exit 0
  fi
fi
echo "worktree-teardown: active sessions protected from GC: $(printf '%s' "$ACTIVE_SHORTS" | tr '\n' ' ')"

is_active_short() {
  case " $(printf '%s' "$ACTIVE_SHORTS" | tr '\n' ' ') " in
    *" $1 "*) return 0 ;;
  esac
  return 1
}

SWEPT=0
KEPT=0
for ref in $(git -C "$REPO_TOP" for-each-ref --format='%(refname:short)' 'refs/heads/shipcheck/*'); do
  short="${ref#shipcheck/}"
  if is_active_short "$short"; then
    echo "  keep $ref (session still active)"; KEPT=$((KEPT + 1)); continue
  fi
  # v1.49.75: ancestry alone is the WRONG merged-test. CRUX merges by **rebase-merge**, which
  # replays the patch onto the base and mints a new sha — so `--is-ancestor` can NEVER hold for a
  # CR that actually shipped, and every shipped branch was KEPT forever. Observed: session
  # c15665bf shipped, its `de090d9` landed upstream as `637b7083` (identical patch-id), and
  # `shipcheck/c15665bf-cr2` sat here indefinitely.
  #
  # Fall back to patch equivalence, which is what actually answers "did this change land":
  #   rc 0 = on-base or rewritten (landed) | rc 3 = absent (genuinely unmerged) | rc 4 = unknown
  # Fail-safe: on rc 4 (sha gc'd / unreadable) or a missing resolver we KEEP the branch. Deleting
  # on "I could not tell" is exactly the wrong direction for a destructive sweep.
  if ! git -C "$REPO_TOP" merge-base --is-ancestor "$ref" "$BASE_REF" 2>/dev/null; then
    RESOLVER="$(dirname "$0")/resolve-merged-sha.py"
    LANDED=false
    if [ -x "$RESOLVER" ] || [ -f "$RESOLVER" ]; then
      if python3 "$RESOLVER" --repo "$REPO_TOP" --sha "$ref" --base "$BASE_REF" >/dev/null 2>&1; then
        LANDED=true
      fi
    else
      echo "  keep $ref (resolver missing: $RESOLVER — refusing to guess)"
      KEPT=$((KEPT + 1)); continue
    fi
    if [ "$LANDED" != true ]; then
      echo "  keep $ref (not merged into $BASE_REF — no patch-equivalent commit either)"
      KEPT=$((KEPT + 1))
      continue
    fi
    echo "  $ref landed via rebase-merge (patch-equivalent on $BASE_REF, sha rewritten)"
  fi
  # Layout-agnostic (v1.49.66): same discovery as Mode A, so a legacy B/C tree under an
  # ad-hoc root is now reachable instead of being missed by a hard-coded path shape.
  wts="$(discover_worktrees "$short")"
  echo "  sweep $ref (merged, no active session)"
  if [ -z "$wts" ]; then
    # Branch with no worktree left — drop the ref so it stops accumulating.
    remove_one "" "$ref" "" "$REPO_TOP" && SWEPT=$((SWEPT + 1)) || KEPT=$((KEPT + 1))
    continue
  fi
  gc_failed=0
  OLD_IFS="$IFS"; IFS='
'
  for wt in $wts; do
    IFS="$OLD_IFS"
    owner="$(owner_repo_of "$wt" || true)"; [ -n "$owner" ] || owner="$REPO_TOP"
    remove_one "$wt" "$ref" "$(skeleton_of "$wt")" "$owner" || gc_failed=1
    IFS='
'
  done
  IFS="$OLD_IFS"
  sess="${ROOT_BASE}/${short}"
  if [ "$gc_failed" = 0 ] && [ -d "$sess" ] && ! already_rmed "$sess" && safe_to_rm "$sess" "$short"; then
    run rm -rf "$sess"
  fi
  if [ "$gc_failed" = 0 ]; then SWEPT=$((SWEPT + 1)); else KEPT=$((KEPT + 1)); fi
done

# ---------------------------------------------------------------------------
# GC pass 2 (v1.49.66): orphan DIRECTORY residue.
#
# Pass 1 is branch-driven, so it can only ever see dirs whose `shipcheck/*` ref still exists.
# Residue with no ref at all — an ad-hoc root left behind, or the observed
# `state/worktrees/bff/main/` — was unreachable by any sweep and simply accumulated.
#
# Also scans SIBLING roots (`${ROOT_BASE}*`, e.g. `worktrees-fe`): those are separate
# top-level dirs, so a sweep (or a `du`) of ROOT_BASE alone silently under-counts them —
# that is precisely how half of the measured 1.85G stayed invisible.
#
# Only ever removes a tree containing NO `.git` anywhere: if a live worktree is inside, it is
# not residue and pass 1 owns it.
# ---------------------------------------------------------------------------
ORPHANED=0
for base in "${ROOT_BASE}"*; do
  [ -d "$base" ] || continue
  for cand in "$base"/*; do
    [ -d "$cand" ] || continue
    name="$(basename "$cand")"
    if is_active_short "$name"; then
      echo "  keep $cand (active session)"; continue
    fi
    if git -C "$REPO_TOP" show-ref --verify --quiet "refs/heads/shipcheck/${name}"; then
      echo "  keep $cand (branch shipcheck/$name still exists)"; continue
    fi
    if [ -n "$(find "$cand" -maxdepth 4 -name '.git' -print -quit 2>/dev/null)" ]; then
      echo "  keep $cand (contains a live worktree — pass 1 owns it)"; continue
    fi
    case "$cand" in
      "$ROOT_BASE"/*|*/ship-check/state/worktrees*/*) : ;;
      *) echo "  ⚠️ refusing to rm outside a worktrees root: $cand"; continue ;;
    esac
    echo "  orphan-dir $cand ($(du -sh "$cand" 2>/dev/null | cut -f1 || echo '?')) — no ref, no worktree, no active session"
    run rm -rf "$cand"
    ORPHANED=$((ORPHANED + 1))
  done
done

echo "worktree-teardown: GC done — swept=$SWEPT kept=$KEPT orphan_dirs=$ORPHANED"
