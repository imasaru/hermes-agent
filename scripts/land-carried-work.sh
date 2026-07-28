#!/usr/bin/env bash
#
# land-carried-work.sh
#
# Pulls latest from your fork + lands carried feature work via cherry-pick.
#
# Features:
# - Content-aware dedup using ancestor + git cherry (catches same work, different SHAs)
# - scripts/ignored-carried-shas.txt for permanently superseded commits
# - Auto-skips merge commits
# - Optional --rebase-carried to bring the source branches up to date after landing
#
# Safe to re-run.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BRANCH_LIST_FILE="scripts/carried-branches.txt"
IGNORED_SHAS_FILE="scripts/ignored-carried-shas.txt"

DRY_RUN=false
DO_PUSH=false
DO_REBASE=false
OVERRIDE_BRANCHES=()
USE_OVERRIDE=false

print_help() {
    cat <<'EOF'
land-carried-work.sh - fork update + cherry-pick carried branches onto main

Improved deduplication:
  * git cherry content equivalence (different SHAs, same changes)
  * scripts/ignored-carried-shas.txt
  * auto-skip merge commits
  * --rebase-carried to update the source branches

Usage:
  ./scripts/land-carried-work.sh
  ./scripts/land-carried-work.sh --plan
  ./scripts/land-carried-work.sh --push --rebase-carried
  ./scripts/land-carried-work.sh --branches feat/foo local/macmini
EOF
}

# ---------- Helper functions (defined early) ----------

is_merge_commit() {
    local c="$1"
    local num_parents
    num_parents=$(git cat-file -p "$c" 2>/dev/null | grep -c '^parent ' || echo 0)
    [[ $num_parents -gt 1 ]]
}

is_already_landed() {
    local c="$1"

    if git merge-base --is-ancestor "$c" HEAD 2>/dev/null; then
        return 0
    fi

    # "-" from git cherry means the patch is already present in HEAD
    if git cherry HEAD "$c" 2>/dev/null | grep -q '^-' ; then
        return 0
    fi

    if [[ -f "$IGNORED_SHAS_FILE" ]] && grep -qxF "$c" "$IGNORED_SHAS_FILE" 2>/dev/null; then
        return 0
    fi

    return 1
}

rebase_carried_branches() {
    echo ""
    echo "==> Rebasing carried branches onto current main..."

    local original_branch
    original_branch=$(git symbolic-ref --short HEAD 2>/dev/null || echo "main")

    local had_issues=false

    for branch in "${CARRIED_BRANCHES[@]}"; do
        if ! git show-ref --verify --quiet "refs/heads/$branch"; then
            continue
        fi

        echo "  → $branch"
        git checkout "$branch" >/dev/null 2>&1 || continue

        if git rebase main >/dev/null 2>&1; then
            echo "    ✓ rebased cleanly"
        else
            echo "    ! rebase stopped"
            echo "      You can: git rebase --skip   (for already-landed dups)"
            echo "      then:   git rebase --continue"
            echo "      or:     git rebase --abort"
            had_issues=true
            break
        fi
    done

    git checkout "$original_branch" >/dev/null 2>&1 || git checkout main

    if $had_issues; then
        echo ""
        echo "Rebase(s) need manual attention. Finish the rebase, then re-run the script."
    else
        echo "    All carried branches are now up to date with main."
    fi
}

# ---------- Argument parsing ----------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --plan|-n|--dry-run)
            DRY_RUN=true
            shift
            ;;
        --push)
            DO_PUSH=true
            shift
            ;;
        --rebase-carried)
            DO_REBASE=true
            shift
            ;;
        --branches)
            USE_OVERRIDE=true
            shift
            while [[ $# -gt 0 && "$1" != --* ]]; do
                OVERRIDE_BRANCHES+=("$1")
                shift
            done
            ;;
        -h|--help)
            print_help
            exit 0
            ;;
        --*)
            echo "Unknown flag: $1" >&2
            print_help
            exit 1
            ;;
        *)
            OVERRIDE_BRANCHES+=("$1")
            USE_OVERRIDE=true
            shift
            ;;
    esac
done

load_carried_branches() {
    local -a branches=()

    if $USE_OVERRIDE && [[ ${#OVERRIDE_BRANCHES[@]} -gt 0 ]]; then
        branches=("${OVERRIDE_BRANCHES[@]}")
        printf '%s\n' "${branches[@]}"
        return 0
    fi

    if [[ -f "$BRANCH_LIST_FILE" ]]; then
        while IFS= read -r raw || [[ -n "$raw" ]]; do
            line="${raw%%#*}"
            line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
            [[ -n "$line" ]] && branches+=("$line")
        done < "$BRANCH_LIST_FILE"
    fi

    if $USE_OVERRIDE && [[ ${#OVERRIDE_BRANCHES[@]} -gt 0 ]]; then
        branches+=("${OVERRIDE_BRANCHES[@]}")
    fi

    if [[ ${#branches[@]} -eq 0 ]]; then
        branches=(
            "feat/kanban-approval-flow"
            "local/macmini"
        )
    fi

    printf '%s\n' "${branches[@]}"
}

# ---------- Main flow ----------

echo "==> Fetching remotes..."
git fetch origin --prune
git fetch upstream --prune

current=$(git symbolic-ref --short HEAD 2>/dev/null || echo "DETACHED")
if [[ "$current" != "main" ]]; then
    echo "==> Switching to main (was on $current)"
    git checkout main
fi

echo "==> Fast-forwarding main from origin/main..."
if ! git pull --ff-only origin main; then
    echo "ERROR: Could not fast-forward main." >&2
    echo "Resolve local commits first, then re-run." >&2
    exit 1
fi

echo ""
echo "==> Loading carried branches..."

CARRIED_BRANCHES=()
while IFS= read -r b; do
    [[ -n "$b" ]] && CARRIED_BRANCHES+=("$b")
done < <(load_carried_branches)

echo "    Branches (preference order, earlier wins on duplicates):"
for b in "${CARRIED_BRANCHES[@]}"; do
    echo "      • $b"
done

if [[ -f "$IGNORED_SHAS_FILE" ]]; then
    echo "    (using ignore list: $IGNORED_SHAS_FILE)"
fi

echo ""
echo "==> Finding commits that are on these branches but not on main yet..."

SEEN_FILE=$(mktemp)
trap 'rm -f "$SEEN_FILE"' EXIT

to_pick=()
skipped_already=0
skipped_merge=0

for branch in "${CARRIED_BRANCHES[@]}"; do
    if ! git show-ref --verify --quiet "refs/heads/$branch"; then
        echo "  (branch missing locally, skipping: $branch)"
        continue
    fi

    while IFS= read -r c; do
        [[ -z "$c" ]] && continue

        if is_already_landed "$c"; then
            skipped_already=$((skipped_already + 1))
            continue
        fi

        if is_merge_commit "$c"; then
            echo "  - skipping merge commit: $c"
            skipped_merge=$((skipped_merge + 1))
            echo "$c" >> "$SEEN_FILE"
            continue
        fi

        if grep -qxF "$c" "$SEEN_FILE" 2>/dev/null; then
            continue
        fi

        echo "$c" >> "$SEEN_FILE"

        subj=$(git log -1 --pretty=%s "$c" | cut -c1-70)
        printf "  + %s  %s\n" "$c" "$subj"
        to_pick+=("$c")
    done < <(git rev-list --reverse HEAD.."$branch")
done

if [[ $skipped_already -gt 0 || $skipped_merge -gt 0 ]]; then
    echo ""
    echo "    (skipped $skipped_already already-landed/ignored + $skipped_merge merge commits)"
fi

if [[ ${#to_pick[@]} -eq 0 ]]; then
    echo ""
    echo "Nothing new to cherry-pick. All carried work is already on main."

    if $DO_REBASE; then
        rebase_carried_branches
    fi

    if $DO_PUSH; then
        echo "==> Pushing origin/main..."
        git push origin main
    fi
    exit 0
fi

echo ""
echo "==> ${#to_pick[@]} commit(s) will be landed."
echo ""

if $DRY_RUN; then
    echo "(plan only) Run without --plan to actually cherry-pick."
    exit 0
fi

echo "==> Cherry-picking in order..."
echo "    On conflict: resolve → git cherry-pick --continue → re-run this script"
echo ""

idx=0
total=${#to_pick[@]}

for commit in "${to_pick[@]}"; do
    idx=$((idx + 1))
    subj=$(git log -1 --pretty=%s "$commit" | cut -c1-68)
    printf "[%d/%d] %s  %s\n" "$idx" "$total" "$commit" "$subj"

    if git cherry-pick "$commit"; then
        echo "        OK"
        continue
    fi

    echo ""
    echo "CONFLICT on $commit"
    echo ""
    echo "Resolve the files, then pick one:"
    echo "    git cherry-pick --continue"
    echo "    git cherry-pick --skip"
    echo "    git cherry-pick --abort"
    echo ""
    echo "After that, run:"
    echo "    ./scripts/land-carried-work.sh"
    exit 1
done

echo ""
echo "==> Success! HEAD is now:"
git log --oneline -1

if $DO_PUSH; then
    echo "==> Pushing to origin/main..."
    git push origin main
    echo "    Pushed."
fi

if $DO_REBASE; then
    rebase_carried_branches
fi

echo ""
echo "Next step recommendation:  hermes update"
