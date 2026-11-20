"""P1: 디스크를 한 번 읽어 JPEG/RAW/영상 후보의 위치·길이·종류·촬영일을 목록으로 만든다.

- 장치는 읽기 전용으로만 연다. 결과(session 폴더)는 저장소 밖에만 쓴다.
- 4096 바이트로 정렬된 블록의 시작만 후보로 본다(APFS 파일 데이터는 블록 정렬).
- 이미 찾은 파일 안쪽은 건너뛴다(큰 영상은 읽지 않고 넘어가서 빠르다).
- progress.json 으로 이어하기. 장치가 사라지면(ENXIO 등) 즉시 멈추고 이어할 위치를 남긴다.
"""
import errno
import json
import os
import shutil
import time
from collections import Counter

from .blockdev import ReadOnlyDevice
from .formats import (bmff_meta, bmff_probe, classify_bmff, jpeg_meta, jpeg_probe)
from .scan import BS, CHUNK, REPO

MIN_JPEG = 20_000
MIN_BMFF = 50_000
MAX_BMFF = 64 << 30


def _align_up(x, a=BS):
    return (x + a - 1) // a * a


def _check_session(session):
    session = os.path.realpath(os.path.expanduser(session))
    if session == REPO or session.startswith(REPO + os.sep):
        raise ValueError("세션 폴더가 저장소 안입니다: %s" % session)
    os.makedirs(session, exist_ok=True)
    if shutil.disk_usage(session).free < 1 << 30:
        raise ValueError("세션 폴더의 여유 공간이 1GB 미만입니다")
    return session


def catalog(path, session, start_block, end_block, on_progress=None, should_cancel=None):
    """반환 코드: 0 완료, 3 취소, 4 장치 끊김."""
    session = _check_session(session)
    cat_path = os.path.join(session, "catalog.jsonl")
    prog_path = os.path.join(session, "progress.json")
    pos, next_id, found = start_block * BS, 1, 0
    if os.path.exists(prog_path) and os.path.exists(cat_path):
        st = json.load(open(prog_path))
        if st.get("start_block") == start_block and st.get("end_block") == end_block:
            pos, next_id, found = st["next_pos"], st["next_id"], st["found"]
            print("이어하기: 오프셋 %d (%.1f GB)부터" % (pos, pos / 1e9))
    elif os.path.exists(cat_path):
        os.remove(cat_path)
    end = end_block * BS
    skip_until = pos
    t0, read_bytes, consecutive = time.time(), 0, 0

    def save(next_pos, state):
        tmp = prog_path + ".tmp"
        json.dump({"start_block": start_block, "end_block": end_block, "next_pos": next_pos,
                   "next_id": next_id, "found": found, "state": state, "updated": time.time()},
                  open(tmp, "w"))
        os.replace(tmp, prog_path)

    result = 0
    with ReadOnlyDevice(path) as dev, open(cat_path, "a") as out:
        while pos < end:
            if should_cancel and should_cancel():
                save(pos, "cancelled"); result = 3; break
            n = min(CHUNK, end - pos)
            try:
                buf = dev.read_at(pos, n)
                consecutive = 0
            except OSError as e:
                consecutive += 1
                print("\n읽기 오류 offset %d: %s" % (pos, e), flush=True)
                if e.errno in (errno.ENXIO, errno.ENODEV) or consecutive >= 5:
                    save(pos, "disconnected"); result = 4
                    print("장치 연결이 끊긴 것으로 보여 중단합니다. 같은 명령으로 이어서 실행하세요.")
                    break
                pos += n
                continue
            if not buf:
                break
            hits = []
            for sig, kind, delta in ((b"\xff\xd8\xff", "jpg", 0), (b"ftyp", "bmff", 4)):
                i = buf.find(sig)
                while i != -1:
                    if (i - delta) % BS == 0:
                        hits.append((i - delta, kind))
                    i = buf.find(sig, i + 1)
            for rel, kind in sorted(hits):
                off = pos + rel
                if off < skip_until:
                    continue
                rec = _probe(dev, off, kind, end - off)
                if not rec:
                    continue
                rec["id"], rec["offset"] = next_id, off
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                next_id += 1
                found += 1
                skip_until = off + rec["length"]
            out.flush()
            read_bytes += len(buf)
            pos += len(buf)
            if skip_until > pos:
                pos = min(end, _align_up(skip_until))
            save(pos, "running")
            if on_progress:
                el = max(time.time() - t0, 1e-9)
                on_progress({"pos": pos, "total": end, "found": found,
                             "mb_s": read_bytes / 1e6 / el})
            else:
                print("\r%.1f%% %.1f GB, 발견 %d" % (100 * pos / end, pos / 1e9, found),
                      end="", flush=True)
        else:
            save(pos, "done")
    print()
    return result


def _probe(dev, off, kind, room):
    if kind == "jpg":
        r = jpeg_probe(dev, off)
        if not r or r[0] < MIN_JPEG:
            return None
        length, info = r
        rec = {"kind": "jpeg", "length": length, "structure": "ok"}
        rec.update(jpeg_meta(info))
        return _final(rec)
    res = bmff_probe(dev, off, min(MAX_BMFF, room))
    if not res or res["length"] < MIN_BMFF:
        return None
    c = classify_bmff(res)
    if not c:
        return None
    k, structure = c
    rec = {"kind": k, "length": res["length"], "structure": structure}
    rec.update(bmff_meta(k, res["moov"]))
    return _final(rec)


def _final(rec):
    rec.setdefault("taken", None)
    rec.setdefault("model", None)
    return rec


def summarize(session):
    """catalog.jsonl 을 요약한다 (UI 결과 화면용)."""
    session = os.path.realpath(os.path.expanduser(session))
    kinds = Counter()
    sizes = Counter()
    structure = {}
    months = Counter()
    models = Counter()
    undated = 0
    total = 0
    for line in open(os.path.join(session, "catalog.jsonl")):
        r = json.loads(line)
        total += 1
        kinds[r["kind"]] += 1
        sizes[r["kind"]] += r["length"]
        structure.setdefault(r["kind"], Counter())[r["structure"]] += 1
        if r.get("taken"):
            months[r["taken"][:7]] += 1
        else:
            undated += 1
        if r.get("model"):
            models[r["model"]] += 1
    return {"total": total, "kinds": dict(kinds), "bytes": dict(sizes),
            "structure": {k: dict(v) for k, v in structure.items()},
            "months": dict(sorted(months.items())), "undated": undated,
            "models": dict(models.most_common(10))}


def print_summary(session):
    s = summarize(session)
    print("총 %d개, %.2f GB" % (s["total"], sum(s["bytes"].values()) / 1e9))
    for k in sorted(s["kinds"]):
        print("  %-14s %7d개  %8.2f GB  구조 %s" % (k, s["kinds"][k], s["bytes"][k] / 1e9, s["structure"][k]))
    print("촬영일 없음: %d개" % s["undated"])
    if s["months"]:
        print("촬영 월: %s ~ %s (%d개월)" % (min(s["months"]), max(s["months"]), len(s["months"])))
    print("카메라:", s["models"] or "읽은 것 없음")
    return 0
