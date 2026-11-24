"""세션 기록(검사·꺼내기·확인 파일)과 보고서의 버전 호환.

원칙
- 읽을 때: 빠진 값은 기본값으로 채우고, 모르는 값(새 종류, 추가 항목)은 버리지 말고 그대로 둔다.
  읽을 수 없는 줄·파일은 건너뛰되 몇 개를 건너뛰었는지 센다(조용히 사라지지 않게).
- 쓸 때: 세션 폴더에 session_meta.json(형식 번호)을 남긴다. 번호가 없으면 "이전 버전(1)" 기록으로 본다.
- 더 새 버전이 만든 기록(번호가 현재보다 큼)은 읽기만 하고, 이어서 쓰는 것은 경고한다.
"""
import json
import os
import time

SCHEMA = 2            # 현재 세션 기록 형식. 필드 의미가 바뀌면 올린다.
REPORT_FORMAT = 2     # 보고서 글 형식
TOOL = "RecoveryHDD"
META = "session_meta.json"


def read_json(path, default=None, want=dict):
    """JSON 파일을 읽는다. 없거나 깨졌거나 모양이 다르면 default."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, want) else default


def session_info(session):
    """{'schema': 번호, 'legacy': 이전 버전 기록인가, 'newer': 더 새 버전 기록인가}"""
    meta = read_json(os.path.join(session, META), {})
    n = meta.get("schema")
    if not isinstance(n, int) or n < 1:
        n = 1
    return {"schema": n, "legacy": n < SCHEMA, "newer": n > SCHEMA, "tool": meta.get("tool")}


def stamp(session):
    """세션 폴더에 형식 번호를 남긴다. 이미 같거나 더 새 번호면 건드리지 않는다."""
    info = session_info(session)
    if info["schema"] >= SCHEMA and os.path.exists(os.path.join(session, META)):
        return info
    if info["newer"]:
        return info
    os.makedirs(session, exist_ok=True)
    meta = {"schema": SCHEMA, "tool": TOOL, "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "upgraded_from": info["schema"] if os.path.exists(os.path.join(session, "catalog.jsonl")) else None}
    tmp = os.path.join(session, META + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(session, META))
    return session_info(session)


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def catalog_record(r):
    """검사 기록 한 줄. 항목 번호(id)와 위치(offset)가 없으면 쓸 수 없으니 None."""
    if not isinstance(r, dict) or "id" not in r or "offset" not in r:
        return None
    out = dict(r)  # 모르는 항목은 그대로 둔다
    out["id"] = _int(r["id"], -1)
    out["offset"] = _int(r["offset"], -1)
    if out["id"] < 0 or out["offset"] < 0:
        return None
    out["length"] = max(0, _int(r.get("length")))
    out["kind"] = str(r.get("kind") or "unknown")
    out["structure"] = str(r.get("structure") or "unknown")
    return out


def extracted_record(r):
    if not isinstance(r, dict) or "id" not in r:
        return None
    out = dict(r)
    out["id"] = _int(r["id"], -1)
    if out["id"] < 0:
        return None
    out["path"] = str(r.get("path") or "")
    out["bytes"] = max(0, _int(r.get("bytes")))
    return out


def verify_record(r):
    if not isinstance(r, dict):
        return None
    out = dict(r)
    out["status"] = str(r.get("status") or "unchecked")
    out["path"] = str(r.get("path") or "")
    out["reason"] = str(r.get("reason") or "")
    return out


def verify_summary(s):
    """검증 요약. 빠진 항목은 0/빈 값으로 채운다."""
    if not isinstance(s, dict):
        return None
    out = dict(s)
    for k in ("total", "ok", "fail", "fixed", "unchecked"):
        out[k] = max(0, _int(s.get(k)))
    for k in ("levels", "reasons", "tools"):
        out[k] = s[k] if isinstance(s.get(k), dict) else {}
    out["cancelled"] = bool(s.get("cancelled"))
    return out


def clean_lines(records, normalize, stats=None):
    """records 를 normalize 로 정리해 쓸 수 있는 것만 돌려준다. 버린 개수는 stats['skipped'] 에 더한다."""
    out = []
    for r in records:
        n = normalize(r)
        if n is None:
            if stats is not None:
                stats["skipped"] = stats.get("skipped", 0) + 1
        else:
            out.append(n)
    return out
