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
from .formats import (bmff_meta, bmff_probe, classify_bmff, embedded_thumb, jpeg_meta, jpeg_probe, zip_kind,
                      zip_probe)
from . import formats_more as fm
from . import compat
from .jsonl import read_jsonl
from .ownership import expand
from .scan import BS, CHUNK, REPO
from .timeutil import local_month, to_local

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


THUMBS = "thumbs"  # 세션 폴더 안의 내장 미리보기 폴더: thumbs/<항목 번호>.jpg
SESSION_FILES = ("catalog.jsonl", "progress.json", "extracted.jsonl", "failed.jsonl", "run_state.json",
                 "plan.json", "verify.jsonl", "verify_summary.json", "duplicates.jsonl", "tail_check.json", "ack.json",
                 THUMBS)  # 미리보기도 함께 보관한다(새 검사의 번호와 섞이지 않게)


def thumb_path(session, rid):
    return os.path.join(session, THUMBS, "%d.jpg" % int(rid))


def _save_thumb(session, rid, data):
    """미리보기 한 장을 저장한다. 실패해도 검사는 계속한다(미리보기는 있으면 좋은 것)."""
    path = thumb_path(session, rid)
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _drop_thumbs_from(session, first_id):
    """이어하기: 버린 항목(번호 first_id 이상)의 미리보기를 지운다. 같은 번호가 다른 파일에 다시 붙기 때문이다."""
    d = os.path.join(session, THUMBS)
    if not os.path.isdir(d):
        return
    for name in os.listdir(d):
        stem = name.split(".", 1)[0]
        if stem.isdigit() and int(stem) >= first_id:
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass


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


def catalog(path, session, start_block, end_block, on_progress=None, should_cancel=None, fresh=False, known=None,
            known_dir=None):
    """반환 코드: 0 완료, 3 취소, 4 장치 끊김.
    같은 범위의 진행 기록이 있으면 이어서 한다. 범위가 다르거나 fresh=True 면 옛 세션을 보관(archive)하고 새로 시작한다.
    known: 같은 디스크의 이전 검사에서 이미 찾은 항목(검사 기록 형식). 그 구간은 디스크에서 다시 읽지 않고 건너뛰며,
    지나갈 때 항목을 새 검사 기록에 옮겨 적는다(이어하기와 겹치지 않게 지나간 만큼만 적는다).
    known_dir: known 을 읽은 세션 폴더. 그곳에 미리보기가 있으면 새 번호로 복사한다.
    찾은 사진의 내장 미리보기는 session/thumbs/<번호>.jpg 에 저장하고 기록에 "thumb": true 를 적는다."""
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
            _drop_thumbs_from(session, next_id)
            resumed = True
            print("이어하기: 오프셋 %d (%.1f GB)부터" % (pos, pos / 1e9))
    known_thumbs = os.path.join(known_dir, THUMBS) if known_dir else None
    if not resumed:
        old = archive_session(session)
        if old:
            print("이전 세션 파일을 보관했습니다: %s" % old)
            if known_dir and os.path.realpath(known_dir) == session:
                known_thumbs = os.path.join(old, THUMBS)  # 이전 기록이 이 폴더였다면 미리보기는 방금 보관 폴더로 옮겨졌다
    os.makedirs(os.path.join(session, THUMBS), exist_ok=True)
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
                rec = {f: v for f, v in k.items() if f not in ("id", "offset", "thumb")}
                rec["id"], rec["offset"], rec["reused"] = next_id, k["offset"], True
                if k.get("thumb") and known_thumbs:
                    try:
                        shutil.copyfile(os.path.join(known_thumbs, "%d.jpg" % k["id"]), thumb_path(session, next_id))
                        rec["thumb"] = True
                    except OSError:
                        pass  # 이전 미리보기가 없으면 미리보기 없이 옮긴다
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
                th = rec.pop("_thumb", None)
                if th and _save_thumb(session, next_id, th):  # 기록 줄보다 먼저 쓴다(줄에 thumb 가 있으면 파일도 있다)
                    rec["thumb"] = True
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
        rec["_thumb"] = embedded_thumb("jpeg", info=info)
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
    rec["_thumb"] = embedded_thumb(k, moov=res["moov"])
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


_browse_cache = {}


def browse_items(session):
    """꺼내기 전 탐색 화면용 전체 목록(짧은 형태). 화면이 거르고 묶으므로 한 번에 보낸다(항목당 약 40바이트).
    rows: [번호, 종류, 길이, 현지 촬영 시각|None, 카메라 번호(-1=없음), 의심(0/1), 미리보기(0/1), EXIF 방향(0=모름·그대로)]
    version: 기록 파일이 바뀌면 달라진다(화면이 미리보기 주소에 붙여 옛 미리보기를 캐시에서 쓰지 않게)."""
    session = os.path.realpath(expand(session))
    path = os.path.join(session, "catalog.jsonl")
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    hit = _browse_cache.get(session)
    if hit and hit[0] == key:
        return hit[1]
    stats = {}
    models, model_ix, rows, index = [], {}, [], {}
    for r in compat.clean_lines(read_jsonl(path, stats), compat.catalog_record, stats):
        orient = r.get("orient") if r.get("orient") in (2, 3, 4, 5, 6, 7, 8) else 0
        index[r["id"]] = (r["offset"], r["length"], r["kind"], orient)
        dt = to_local(r.get("taken"), r.get("taken_utc", False))
        m = r.get("model")
        if m and m not in model_ix:
            model_ix[m] = len(models)
            models.append(m)
        rows.append([r["id"], r["kind"], r["length"], dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None,
                     model_ix.get(m, -1) if m else -1, 0 if r["structure"] == "ok" else 1, 1 if r.get("thumb") else 0, orient])
    out = {"fields": ["id", "kind", "length", "taken", "model", "suspect", "thumb", "orient"], "rows": rows, "models": models,
           "skipped": stats.get("skipped", 0), "version": "%d-%d" % (st.st_mtime_ns // 1000000, st.st_size)}
    _browse_cache.clear()  # 세션 하나만 둔다(목록이 클 수 있다)
    _browse_cache[session] = (key, out, index)
    return out


def recent_thumbs(session, n=8, tail_bytes=256 << 10):
    """검사 중 화면의 "방금 찾은 사진" 줄: 기록 파일의 끝부분만 읽어 미리보기가 있는 최근 항목 n개(최신 먼저).
    검사가 쓰고 있는 파일이라 마지막 줄이 덜 써졌을 수 있다(읽지 못한 줄은 건너뛴다). 반환: [{id, kind, orient}]"""
    path = os.path.join(os.path.realpath(expand(session)), "catalog.jsonl")
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            chunk = f.read()
    except OSError:
        return []
    lines = chunk.split(b"\n")
    if size > tail_bytes:
        lines = lines[1:]  # 잘려서 시작한 첫 줄
    out = []
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("thumb") and isinstance(r.get("id"), int):
            out.append({"id": r["id"], "kind": r.get("kind"), "orient": r.get("orient") if r.get("orient") in (3, 6, 8) else 0})
            if len(out) >= n:
                break
    return out


def item_location(session, rid):
    """항목 번호 → (위치, 길이, 종류, EXIF 방향). 없으면 None. browse_items 와 같은 캐시를 쓴다."""
    if browse_items(session) is None:
        return None
    return _browse_cache[os.path.realpath(expand(session))][2].get(rid)


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
