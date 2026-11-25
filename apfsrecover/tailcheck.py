"""끝부분 촘촘 검사: 기본 검사가 읽지 않은 뒤쪽 구간에 데이터가 있는지 확인한다(읽기 전용).

기본 검사는 128MB 마다 4KB 를 찍어 보고 '내용이 있는 마지막 위치 + 여유'까지만 읽는다.
여기서는 그 뒤쪽을 더 촘촘한 간격(기본 32MB)으로 4KB 씩 찍어 본다.
- 내용 있음: 4KB 가 전부 0 이 아니다(지워진 파일의 찌꺼기일 수도 있다).
- 파일 시작 표시: 찍은 4KB 가 마침 아는 파일 종류의 시작이다(우연히 맞을 때만 보이므로 드물다).
간격보다 작은 파일이 따로 떨어져 있으면 여전히 놓칠 수 있다. 결과는 '추정'이다.

결과·진행 상태는 세션 폴더의 tail_check.json 에 남긴다. 멈췄다가 이어서 할 수 있다.
"""
import json
import os

from . import formats_more as fm

BS = 4096
STATE = "tail_check.json"
GB = 1000 ** 3
MAX_HITS = 200
SAVE_EVERY = 500
# 시작 글자가 짧아 우연히 맞기 쉬운 종류는 더 엄격하게 본다(찌꺼기를 '파일 시작'으로 잘못 세지 않게)
STRICT = {"zip": lambda b: b[:4] == b"PK\x03\x04",
          "bmp": lambda b: b[6:10] == b"\0\0\0\0" and int.from_bytes(b[14:18], "little") in (12, 40, 52, 56, 108, 124)}


def _save(session, st):
    tmp = os.path.join(session, STATE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(session, STATE))


def load(session):
    try:
        with open(os.path.join(session, STATE), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    return st if isinstance(st, dict) and isinstance(st.get("pos"), int) else None


def verdict(st):
    """사람이 읽을 결론 코드: empty | residue | files | partial"""
    if not st or not st.get("finished"):
        return "partial"
    if st.get("hits"):
        return "files"
    return "residue" if st.get("nonzero") else "empty"


def tail_check(dev, session, start, end, step, fingerprint=None, resume=False, on_progress=None, should_cancel=None):
    """dev: read_at(offset, length) 를 가진 읽기 전용 장치. [start, end) 를 step 간격으로 찍어 본다.
    반환: 상태 dict (finished=False 면 취소됨)."""
    start = (start + BS - 1) // BS * BS
    step = max(BS, step // BS * BS)
    prev = load(session) if resume else None
    if prev and (prev.get("start"), prev.get("end"), prev.get("step"), prev.get("fingerprint")) == (start, end, step, fingerprint) \
            and not prev.get("finished"):
        st = prev
    else:
        st = {"start": start, "end": end, "step": step, "fingerprint": fingerprint, "pos": start, "samples": 0,
              "nonzero": 0, "hits": [], "last_data": None, "buckets": {}, "finished": False}
    os.makedirs(session, exist_ok=True)
    n = 0
    off = st["pos"]
    while off + BS <= end:
        if should_cancel and should_cancel():
            st["pos"] = off
            _save(session, st)
            return st
        blk = dev.read_at(off, BS)
        st["samples"] += 1
        if any(blk):
            st["nonzero"] += 1
            st["last_data"] = off
            key = str(off // GB)
            st["buckets"][key] = st["buckets"].get(key, 0) + 1
            k = fm.sniff(blk[:16])
            if k and STRICT.get(k, lambda h: True)(blk) and len(st["hits"]) < MAX_HITS:
                st["hits"].append({"offset": off, "kind": k})
        off += step
        n += 1
        if n % SAVE_EVERY == 0:
            st["pos"] = off
            _save(session, st)
        if on_progress and n % 50 == 0:
            on_progress({"pos": off - start, "total": end - start, "nonzero": st["nonzero"], "hits": len(st["hits"])})
    st["pos"] = end
    st["finished"] = True
    _save(session, st)
    if on_progress:
        on_progress({"pos": end - start, "total": end - start, "nonzero": st["nonzero"], "hits": len(st["hits"])})
    return st
