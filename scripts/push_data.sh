#!/usr/bin/env bash
# 데이터/상태 변경분을 커밋하고 안전하게 push 한다.
#
# 왜 별도 스크립트인가
# --------------------
# `fetch-13f` 와 `poll-daily` 는 같은 브랜치에 push 하고 같은
# `data/state/last_seen.json` 을 건드린다. 기존 구현은 `git pull --rebase
# --autostash` 였는데, 두 워크플로우가 겹치면 이 JSON 에서 충돌이 나고
# rebase 는 자동 해소를 못 해 워크플로우가 하드 실패했다(실제 발생).
#
# 이 스크립트는 충돌을 "실패"가 아니라 "병합해야 할 일"로 다룬다. 상태 파일은
# 단조 증가 집합이므로 정답 병합은 항상 합집합이고, 그 합집합은 순서와 무관하다.
# 상태 파일 외의 충돌은 자동 해소하지 않고 즉시 실패시킨다 — 조용히 덮어쓰는
# 것보다 시끄럽게 멈추는 편이 안전하다.
#
# 사용:
#   scripts/push_data.sh "<커밋 메시지>" <스테이징할 경로...>
# 출력(GITHUB_OUTPUT):
#   committed=true|false          커밋이 만들어졌는지
#   normalized_changed=true|false data/normalized/** 가 바뀌었는지
set -euo pipefail

MSG="${1:?커밋 메시지가 필요합니다}"
shift
PATHS=("$@")
[ "${#PATHS[@]}" -gt 0 ] || PATHS=(data)

STATE="data/state/last_seen.json"
BRANCH="${GITHUB_REF_NAME:-main}"
OUT="${GITHUB_OUTPUT:-/dev/null}"

git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add -A -- "${PATHS[@]}"

if git diff --cached --quiet; then
  echo "::notice::변경 없음 — 빈 커밋을 만들지 않습니다."
  { echo "committed=false"; echo "normalized_changed=false"; } >> "$OUT"
  exit 0
fi

# 대시보드 재빌드 판단용. `last_run` 은 매 실행 바뀌므로 상태 파일 변경만으로
# 재빌드를 걸면 매일 무의미하게 배포가 돈다. 실제 보유내역이 담긴
# data/normalized 가 바뀐 경우에만 true 로 둔다.
if git diff --cached --quiet -- data/normalized; then
  NORMALIZED_CHANGED=false
else
  NORMALIZED_CHANGED=true
fi

git commit -m "$MSG"

resolve_conflicts() {
  local unresolved=0 f
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ "$f" = "$STATE" ]; then
      # rebase 중 :2=onto(원격), :3=재생되는 우리 커밋. 합집합은 대칭이라
      # 어느 쪽을 base 로 두든 결과가 같다.
      git show ":2:$f" > /tmp/state_a.json 2>/dev/null || echo '{}' > /tmp/state_a.json
      git show ":3:$f" > /tmp/state_b.json 2>/dev/null || echo '{}' > /tmp/state_b.json
      python -m src.pipeline.merge_state --into /tmp/state_a.json --with /tmp/state_b.json
      cp /tmp/state_a.json "$f"
      git add "$f"
      echo "  충돌 해소(합집합): $f"
    else
      echo "::error::자동 해소 불가한 충돌: $f"
      unresolved=1
    fi
  done < <(git diff --name-only --diff-filter=U)
  return $unresolved
}

pushed=false
for attempt in 1 2 3 4 5; do
  if git push origin "HEAD:${BRANCH}"; then
    pushed=true
    break
  fi
  echo "  push 거부됨 (시도 ${attempt}/5) — 원격을 가져와 병합합니다."
  git fetch origin "$BRANCH"
  if ! git rebase "origin/${BRANCH}"; then
    if ! resolve_conflicts; then
      git rebase --abort || true
      exit 1
    fi
    # 병합 결과가 원격과 동일하면 재생할 변경이 없어 --continue 가 거부된다.
    GIT_EDITOR=true git rebase --continue || git rebase --skip || true
  fi
  sleep $(( attempt * 3 ))
done

if [ "$pushed" != true ]; then
  echo "::error::5회 시도 후에도 push 하지 못했습니다."
  exit 1
fi

{ echo "committed=true"; echo "normalized_changed=${NORMALIZED_CHANGED}"; } >> "$OUT"
echo "push 완료 (normalized_changed=${NORMALIZED_CHANGED})"
