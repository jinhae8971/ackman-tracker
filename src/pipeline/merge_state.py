"""상태 파일 합집합 병합.

왜 필요한가
-----------
`fetch-13f` 와 `poll-daily` 는 같은 `data/state/{entity}.json` 에 쓴다. 두
워크플로우가 겹치면 `git pull --rebase` 가 이 파일에서 충돌하고, 충돌은
자동 해소되지 않으므로 워크플로우가 하드 실패한다. 실제 첫 운영 실행에서
이 충돌이 발생해 `alerted` 상태가 유실됐다.

상태 파일은 "지금까지 본 accession 의 집합"이라는 단조 증가 자료구조다.
따라서 두 버전의 정답 병합은 **항상 합집합**이며 순서·타이밍과 무관하게
결과가 같다. 이 스크립트가 그 병합을 수행한다.

    python -m src.pipeline.merge_state --into data/state/pershing.json \
        --with /tmp/remote_state.json

`--with` 가 없거나 읽을 수 없으면 아무것도 하지 않고 성공한다(최초 실행).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# 집합 의미론을 갖는 키. 리스트로 저장되지만 순서에 의미가 없다.
SET_KEYS = ("accessions", "alerted")
# 맵 의미론을 갖는 키. 같은 accession 이 양쪽에 있으면 로컬(이번 실행) 값을 쓴다.
MAP_KEYS = ("failed",)


def _load(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[merge_state][WARN] {path} 를 읽지 못했습니다: {exc}",
              file=sys.stderr)
        return {}


def merge(base: dict, other: dict) -> dict:
    out = dict(base)
    for k in SET_KEYS:
        out[k] = sorted(set(base.get(k) or []) | set(other.get(k) or []))
    for k in MAP_KEYS:
        merged = dict(other.get(k) or {})
        merged.update(base.get(k) or {})
        out[k] = merged
    # last_run 은 더 늦은 쪽을 남긴다(ISO8601 은 사전순 = 시간순).
    runs = [str(x) for x in (base.get("last_run"), other.get("last_run")) if x]
    if runs:
        out["last_run"] = max(runs)
    # 위에서 다루지 않은 키는 base 우선으로 보존한다.
    for k, v in other.items():
        out.setdefault(k, v)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="상태 파일 합집합 병합")
    p.add_argument("--into", required=True, help="병합 결과를 쓸 로컬 상태 파일")
    p.add_argument("--with", dest="other", required=True,
                   help="병합해 넣을 다른 버전(예: origin 에서 꺼낸 파일)")
    args = p.parse_args(argv)

    base = _load(args.into)
    other = _load(args.other)
    if not other:
        print("[merge_state] 병합 대상이 비어 있어 그대로 둡니다.")
        return 0

    out = merge(base, other)
    os.makedirs(os.path.dirname(os.path.abspath(args.into)) or ".",
                exist_ok=True)
    with open(args.into, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"[merge_state] 병합 완료 — "
          f"accessions={len(out.get('accessions') or [])} "
          f"alerted={len(out.get('alerted') or [])} "
          f"failed={len(out.get('failed') or {})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
