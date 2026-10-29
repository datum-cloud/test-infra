#!/usr/bin/env bash
#
# test the ensure-repo task: it clones test-infra only into a missing or empty .test-infra, updates only a real
# test-infra clone, and fails on any other directory without touching it. The repository that includes this Taskfile
# keeps its HEAD in every case. Offline: both repositories are local.
#
#   bash hack/ensure-repo.test.sh
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
REF=v0.5.0 # a tag both repositories carry, as Milo and test-infra did when the defect moved a Milo checkout
FAILURES=0

git_quiet() { git -c init.defaultBranch=main -c user.email=test@example.com -c user.name=test "$@" >/dev/null 2>&1; }

# commit_tree <dir> <message> <file>...: commit the files, creating each one
commit_tree() {
  local dir=$1 msg=$2
  shift 2
  for f in "$@"; do mkdir -p "$dir/$(dirname "$f")" && echo "$msg" > "$dir/$f"; done
  git_quiet -C "$dir" add -A && git_quiet -C "$dir" commit -m "$msg"
}

# a test-infra remote: the two files the Taskfile recognises test-infra by, at a tag and one commit after it
make_remote() {
  git_quiet init "$WORK/test-infra-src"
  commit_tree "$WORK/test-infra-src" first cluster/kind-config.yaml components/flux/kustomization.yaml
  git_quiet -C "$WORK/test-infra-src" tag "$REF"
  commit_tree "$WORK/test-infra-src" second cluster/kind-config.yaml
  git_quiet clone --bare "$WORK/test-infra-src" "$WORK/test-infra.git"
}

# an including repository with its own tag of the same name and an origin, as a Milo checkout has
make_includer() {
  local dir=$WORK/$1
  git_quiet init "$dir"
  commit_tree "$dir" old README.md
  git_quiet -C "$dir" tag "$REF"
  commit_tree "$dir" new README.md
  git_quiet clone --bare "$dir" "$dir.git"
  git_quiet -C "$dir" remote add origin "$dir.git"
  echo "$dir"
}

head_of() { echo "$(git -C "$1" rev-parse HEAD) $(git -C "$1" symbolic-ref -q HEAD || echo detached)"; }

# the paths under .test-infra, hashed; a missing directory hashes as empty
tree_of() { (cd "$1" && { find .test-infra -print 2>/dev/null || true; } | sort | shasum); }

run_task() { task --taskfile "$ROOT/Taskfile.yml" --dir "$1" ensure-repo REPO_URL="$WORK/test-infra.git" REPO_REF="$REF"; }

check() {  # check <name> <condition...>
  local name=$1
  shift
  if "$@"; then echo "PASS $name"; else echo "FAIL $name"; FAILURES=$((FAILURES + 1)); fi
}

is_clone_at_ref() { [ "$(git -C "$1/.test-infra" rev-parse HEAD)" = "$(git -C "$WORK/test-infra.git" rev-parse "$REF^{commit}")" ]; }

# case <name> <setup> <expect: clone|refuse>: the includer keeps its HEAD; a clone ends at REF, a refusal fails and
# leaves the directory as it was
case_() {
  local name=$1 setup=$2 expect=$3 dir before ok=0 snapshot
  dir=$(make_includer "$name")
  $setup "$dir"
  before=$(head_of "$dir")
  snapshot=$(tree_of "$dir")
  run_task "$dir" >"$WORK/$name.log" 2>&1 || ok=$?
  check "$name: the including repository keeps its HEAD" [ "$(head_of "$dir")" = "$before" ]
  if [ "$expect" = clone ]; then
    check "$name: the task succeeds" [ "$ok" = 0 ]
    check "$name: .test-infra is test-infra at $REF" is_clone_at_ref "$dir"
  else
    check "$name: the task fails" [ "$ok" != 0 ]
    check "$name: .test-infra is left as it was" [ "$(tree_of "$dir")" = "$snapshot" ]
  fi
}

no_dir() { :; }
empty_dir() { mkdir "$1/.test-infra"; }
stray_file() { mkdir "$1/.test-infra" && echo stray > "$1/.test-infra/kubeconfig"; }
other_repo() { git_quiet init "$1/.test-infra" && commit_tree "$1/.test-infra" other notes.txt; }
real_clone() { git_quiet clone "$WORK/test-infra.git" "$1/.test-infra"; }

make_remote
case_ missing no_dir clone
case_ empty empty_dir clone
case_ stray-file stray_file refuse
case_ other-repo other_repo refuse
case_ real-clone real_clone clone
[ "$FAILURES" = 0 ] || { echo "$FAILURES check(s) failed; logs in a kept copy:"; cp -R "$WORK" "$WORK.kept"; echo "$WORK.kept"; exit 1; }
echo "all ensure-repo checks passed"
