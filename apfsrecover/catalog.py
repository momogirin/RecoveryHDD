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

from . import atomic
from .blockdev import ReadOnlyDevice, device_gone
from .formats import (bmff_meta, bmff_probe, classify_bmff, jpeg_meta, jpeg_probe, zip_kind, zip_probe)
from . import formats_more as fm
from . import compat
from .jsonl import read_jsonl
from .ownership import expand
from .scan import BS, CHUNK, REPO
from .timeutil import local_month

MIN_JPEG = 20_000
MIN_BMFF = 50_000
MIN_ZIP = 4096
MAX_ZIP = 256 << 30
MAX_BMFF = 64 << 30


def _align_up(x, a=BS):
    return (x + a - 1) // a * a


def _check_session(session):
    session = os.path.realpath(expand(session))
    if session == REPO or session.startswith(REPO + os.sep):
        raise ValueError("세션 폴더가 저장소 안입니다: %s" % session)
    os.makedirs(session, exist_ok=True)
    if shutil.disk_usage(session).free < 1 << 30:
        raise ValueError("세션 폴더의 여유 공간이 1GB 미만입니다")
    return session


SESSION_FILES = ("catalog.jsonl", "progress.json", "extracted.jsonl", "failed.jsonl", "run_state.json",
                 "plan.json", "verify.jsonl", "verify_summary.json", "duplicates.jsonl")


def archive_session(session):
    """옛 세션 파일을 지우지 않고 archive_<시각>/ 으로 옮긴다. 옮긴 게 있으면 그 경로를 반환."""
    moving = [f for f in SESSION_FILES if os.path.exists(os.path.join(session, f))]
    if not moving:
        return None
    dst = os.path.join(session, time.strftime("archive_%Y%m%d_%H%M%S"))
    n = 1
    while os.path.exists(dst):
        n += 1
        dst = os.path.join(session, time.strftime("archive_%Y%m%d_%H%M%S") + "_%d" % n)
    os.makedirs(dst)
    for f in moving:
        os.replace(os.path.join(session, f), os.path.join(dst, f))
    return dst


def _repair_catalog(cat_path, pos):
    """갑자기 꺼진 뒤 이어하기: 진행 위치(pos) 이후에 적힌 항목과 잘린 줄을 버린다.
    (그 구간은 이어서 다시 읽으므로, 남겨 두면 같은 파일이 두 번 기록된다.)"""
    recs = [r for r in read_jsonl(cat_path) if r.get("offset", 0) < pos]
    tmp = cat_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    atomic.replace(tmp, cat_path)
    return len(recs), (max(r["id"] for r in recs) + 1 if recs else 1)


def catalog(path, session, start_block, end_block, on_progress=None, should_cancel=None, fresh=False, known=None):
    """반환 코드: 0 완료, 3 취소, 4 장치 끊김.
    같은 범위의 진행 기록이 있으면 이어서 한다. 범위가 다르거나 fresh=True 면 옛 세션을 보관(archive)하고 새로 시작한다.
    known: 같은 디스크의 이전 검사에서 이미 찾은 항목(검사 기록 형식). 그 구간은 디스크에서 다시 읽지 않고 건너뛰며,
    지나갈 때 항목을 새 검사 기록에 옮겨 적는다(이어하기와 겹치지 않게 지나간 만큼만 적는다)."""
    session = _check_session(session)
    cat_path = os.path.join(session, "catalog.jsonl")
    prog_path = os.path.join(session, "progress.json")
    pos, next_id, found = start_block * BS, 1, 0
    resumed = False
    if not fresh and os.path.exists(prog_path) and os.path.exists(cat_path):
        st = compat.read_json(prog_path, {})  # 깨진 진행 기록이면 이어하지 않고 새로 시작한다(옛 기록은 보관)
        if st.get("start_block") == start_block and st.get("end_block") == end_block and isinstance(st.get("next_pos"), int):
            pos = st["next_pos"]
            found, next_id = _repair_catalog(cat_path, pos)
            resumed = True
            print("이어하기: 오프셋 %d (%.1f GB)부터" % (pos, pos / 1e9))
    if not resumed:
        old = archive_session(session)
        if old:
            print("이전 세션 파일을 보관했습니다: %s" % old)
    end = end_block * BS
    skip_until = pos
    t0, read_bytes, consecutive = time.time(), 0, 0
    kn = sorted((k for k in (known or []) if k["length"] > 0 and k["offset"] % BS == 0), key=lambda k: k["offset"])
    ki = 0
    while ki < len(kn) and kn[ki]["offset"] < pos:  # 이어하기: 이미 지나간 것은 새 기록에 들어 있다
        ki += 1
    reused = 0

    def save(next_pos, state):
        tmp = prog_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"start_block": start_block, "end_block": end_block, "next_pos": next_pos,
                       "next_id": next_id, "found": found, "state": state, "updated": time.time()}, f)
        atomic.replace(tmp, prog_path)

    result = 0
    with ReadOnlyDevice(path) as dev, open(cat_path, "a", encoding="utf-8") as out:
        while pos < end:
            if should_cancel and should_cancel():
                save(pos, "cancelled"); result = 3; break
            carried = False
            while ki < len(kn) and kn[ki]["offset"] <= pos:  # 이미 아는 파일: 읽지 않고 건너뛴다
                k = kn[ki]
                ki += 1
                kend = k["offset"] + k["length"]
                if kend <= pos or k["offset"] < skip_until or kend > end:  # 이미 지나갔거나 새로 찾은 것과 겹침
                    continue
                rec = {f: v for f, v in k.items() if f not in ("id", "offset")}
                rec["id"], rec["offset"], rec["reused"] = next_id, k["offset"], True
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                next_id += 1
                found += 1
                reused += 1
                skip_until = kend
                pos = min(end, _align_up(kend))
                carried = True
            if carried:
                out.flush()
                save(pos, "running")
                if on_progress:
                    on_progress({"pos": pos, "total": end, "found": found, "mb_s": read_bytes / 1e6 / max(time.time() - t0, 1e-9)})
                continue
            n = min(CHUNK, end - pos)
            if ki < len(kn):
                n = min(n, kn[ki]["offset"] - pos)  # 아는 파일이 시작하기 전까지만 읽는다
            try:
                buf = dev.read_at(pos, n)
                consecutive = 0
            except OSError as e:
                consecutive += 1
                print("\n읽기 오류 offset %d: %s" % (pos, e), flush=True)
                if device_gone(e) or consecutive >= 5:
                    save(pos, "disconnected"); result = 4
                    print("장치 연결이 끊긴 것으로 보여 중단합니다. 같은 명령으로 이어서 실행하세요.")
                    break
                pos += n
                continue
            if not buf:
                break
            hits = _block_hits(buf)
            for rel, kind in hits:
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
    if reused:
        print("이전 검사 결과에서 %d개를 디스크에서 다시 읽지 않고 가져왔습니다." % reused)
    return result


def _block_hits(buf):
    """4096 바이트로 정렬된 블록의 시작만 보고 후보 (상대 위치, 종류)를 모은다. 종류가 늘어도 한 번만 훑는다."""
    hits = []
    for rel in range(0, len(buf), BS):
        k = fm.sniff(buf[rel:rel + 16])
        if k:
            hits.append((rel, k))
    return hits


def _probe(dev, off, kind, room):
    if kind in fm.PROBES:
        rec = fm.probe(kind, dev, off, room)
        return _final(rec) if rec else None
    if kind == "zip":
        z = zip_probe(dev, off, min(MAX_ZIP, room))
        if not z or z["length"] < MIN_ZIP:
            return None
        return _final({"kind": zip_kind(z["names"]), "length": z["length"],
                       "structure": "ok" if z["complete"] else "suspect", "entries": z["entries"]})
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
    session = os.path.realpath(expand(session))
    kinds = Counter()
    sizes = Counter()
    structure = {}
    months = Counter()
    models = Counter()
    undated = 0
    total = 0
    stats = {}
    for r in compat.clean_lines(read_jsonl(os.path.join(session, "catalog.jsonl"), stats), compat.catalog_record, stats):
        total += 1
        kinds[r["kind"]] += 1
        sizes[r["kind"]] += r["length"]
        structure.setdefault(r["kind"], Counter())[r["structure"]] += 1
        month = local_month(r)
        if month:
            months[month] += 1
        else:
            undated += 1
        if r.get("model"):
            models[r["model"]] += 1
    return {"total": total, "kinds": dict(kinds), "bytes": dict(sizes),
            "structure": {k: dict(v) for k, v in structure.items()},
            "months": dict(sorted(months.items())), "undated": undated,
            "models": dict(models.most_common(10)), "skipped": stats.get("skipped", 0)}


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
