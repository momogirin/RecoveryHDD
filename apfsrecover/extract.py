"""P4: 복구 계획 적용과 묶음 단위 추출.

- 원본 장치는 읽기 전용으로만 연다. 결과 파일은 저장 폴더(가드를 통과한 곳)에만 쓴다.
- 각 파일은 `.part` 로 쓰고 다 쓰면 이름을 바꾼다. 중간에 끊겨도 미완성 파일이 완성본처럼 남지 않는다.
- 완료한 파일은 세션의 extracted.jsonl 에 기록한다. 같은 계획으로 다시 시작하면 이어서 한다.
"""
import errno
import hashlib
import json
import os
import shutil
import time

from . import atomic
from .blockdev import ReadOnlyDevice, device_gone
from .guards import check_dest, margin_for
from . import compat
from . import formats_more as fm
from .jsonl import read_jsonl
from .scan import BS
from .timeutil import local_date

KIND_EXT = {"jpeg": "jpg", "raw-cr3": "cr3", "video-mp4": "mp4", "video-mov": "mov",
            "heic": "heic", "unknown-bmff": "bin", "zip": "zip", "docx": "docx", "xlsx": "xlsx", "pptx": "pptx",
            **fm.KIND_EXT}
COPY_CHUNK = 8 * 1024 * 1024
READ_TRIES = 3          # 읽기 오류(EIO)는 이만큼 다시 시도한다
RETRY_DELAY = 0.5       # 초. 시도마다 늘어난다
MAX_CONSECUTIVE_FAILS = 5   # 연속으로 이만큼 못 읽으면 디스크가 불안정하다고 보고 멈춘다
RECHECK_LAST = 20       # 비정상 종료 뒤 이어할 때 마지막으로 기록된 파일을 다시 확인하는 개수
UNCLEAN = ("running", "disconnected", "error", "nospace", "unstable")
UNDATED = "날짜불명"
SUSPECT_DIR = "_의심"
# 결정(01-plan.md 7절): 종류별 폴더 아래에 날짜별. 구조 점검이 '의심'인 파일은 종류 폴더 안의 _의심 에 따로 둔다.
KIND_DIR = {"jpeg": "사진", "heic": "사진", "raw-cr3": "RAW", "video-mp4": "영상", "video-mov": "영상",
            "unknown-bmff": "기타", "zip": "압축", "docx": "문서", "xlsx": "문서", "pptx": "문서", **fm.KIND_DIR}


def load_catalog(session, stats=None):
    """검사 기록. 다른 버전이 만든 기록도 읽도록 빠진 값은 채우고, 쓸 수 없는 줄은 건너뛴다(stats['skipped'])."""
    return compat.clean_lines(read_jsonl(os.path.join(session, "catalog.jsonl"), stats), compat.catalog_record, stats)


def load_extracted(session):
    path = os.path.join(session, "extracted.jsonl")
    if not os.path.exists(path):
        return {}
    done = {}
    for r in compat.clean_lines(read_jsonl(path), compat.extracted_record):
        if r.get("invalid"):  # 이어하기 때 다시 확인해 보니 파일이 이상해서 무효로 표시한 항목
            done.pop(r["id"], None)
        else:
            done[r["id"]] = r
    return done


def select(records, plan):
    """계획의 필터(종류, 날짜, 의심 포함)에 맞는 항목을 오프셋 순으로 반환."""
    kinds = set(plan.get("kinds") or [])
    inc_suspect = bool(plan.get("include_suspect"))
    inc_undated = plan.get("include_undated", True)
    d_from, d_to = plan.get("date_from"), plan.get("date_to")
    out = []
    for r in records:
        if r["kind"] not in kinds:
            continue
        if r["structure"] != "ok" and not inc_suspect:
            continue
        d = local_date(r)
        if d is None:
            if not inc_undated:
                continue
        else:
            if d_from and d < d_from:
                continue
            if d_to and d > d_to:
                continue
        out.append(r)
    out.sort(key=lambda r: r["offset"])
    return out


def dest_relpath(rec):
    ext = KIND_EXT.get(rec["kind"], "bin")
    day = local_date(rec) or UNDATED
    parts = [KIND_DIR.get(rec["kind"], "기타")]
    if rec.get("structure") != "ok":
        parts.append(SUSPECT_DIR)
    parts.append(day)
    parts.append("%s_%06d_blk%d.%s" % (ext, rec["id"], rec["offset"] // BS, ext))
    return os.path.join(*parts)


def next_batch(selected, done_ids, batch):
    """아직 안 꺼낸 항목에서 묶음 한도(GB 또는 개수)만큼 앞에서부터 담는다."""
    size = float(batch.get("size") or 0)
    if size <= 0:
        size = 1
    by_gb = batch.get("unit", "gb") == "gb"
    limit = size * 1000**3 if by_gb else size
    pending = [r for r in selected if r["id"] not in done_ids]
    out, acc = [], 0
    for r in pending:
        cost = r["length"] if by_gb else 1
        if out and acc + cost > limit:
            break
        out.append(r)
        acc += cost
    return out


def load_failed(session):
    path = os.path.join(session, "failed.jsonl")
    return {r["id"] for r in compat.clean_lines(read_jsonl(path), compat.extracted_record)} if os.path.exists(path) else set()


def load_dups(session):
    """내용이 이미 꺼낸 파일과 같아서 건너뛴 항목(duplicates.jsonl): {id: 같은 내용의 파일 경로}"""
    path = os.path.join(session, "duplicates.jsonl")
    return {r["id"]: r.get("duplicate_of", "") for r in compat.clean_lines(read_jsonl(path), compat.extracted_record)} if os.path.exists(path) else {}


def overview(records, plan, done, failed=frozenset(), dups=frozenset()):
    sel = select(records, plan)
    skip = set(done) | set(failed) | set(dups)
    pending = [r for r in sel if r["id"] not in skip]
    nb = next_batch(sel, skip, plan.get("batch", {"unit": "gb", "size": 50}))
    batches = 0
    rest = list(sel)
    seen = set(skip)
    while True:
        b = next_batch(rest, seen, plan.get("batch", {"unit": "gb", "size": 50}))
        if not b:
            break
        seen |= {r["id"] for r in b}
        batches += 1
    return {"files": len(sel), "bytes": sum(r["length"] for r in sel),
            "done_files": sum(1 for r in sel if r["id"] in done),
            "failed_files": sum(1 for r in sel if r["id"] in failed),
            "duplicate_files": sum(1 for r in sel if r["id"] in dups), "pending_files": len(pending),
            "pending_bytes": sum(r["length"] for r in pending),
            "next_files": len(nb), "next_bytes": sum(r["length"] for r in nb),
            "batches_remaining": batches}


def check_plan(session, plan, forbidden=()):
    records = load_catalog(session)
    done = load_extracted(session)
    ov = overview(records, plan, done, load_failed(session), load_dups(session))
    res = check_dest(plan.get("dest"), forbidden, ov["next_bytes"])
    if not plan.get("kinds"):
        res["ok"] = False
        res["reasons"].insert(0, "복구할 종류를 하나 이상 고르세요.")
    elif ov["files"] == 0:
        res["ok"] = False
        res["reasons"].insert(0, "선택한 조건에 맞는 파일이 없습니다.")
    elif ov["pending_files"] == 0:
        res["ok"] = False
        res["reasons"].insert(0, "이 계획의 파일을 모두 꺼냈습니다.")
    res["overview"] = ov
    return res


class SourceGone(Exception):
    """원본 장치가 사라짐(케이블 분리 등)."""


class SourceUnreadable(Exception):
    """원본의 이 위치를 읽지 못함(불량 섹터 등). 장치는 아직 있다."""


def _read(dev, off, ln):
    last = None
    for i in range(READ_TRIES):
        try:
            data = dev.read_at(off, ln)
            if data:
                return data
            last = OSError(errno.EIO, "빈 응답")
        except OSError as e:
            if device_gone(e):
                raise SourceGone() from e
            last = e
        if i + 1 < READ_TRIES:
            time.sleep(RETRY_DELAY * (i + 1))
    raise SourceUnreadable(str(last))


def _fsync(f):
    """전원이 갑자기 나가도 '저장됨'으로 기록한 파일이 덜 써져 있지 않도록 디스크에 확실히 쓴다."""
    f.flush()
    try:
        import fcntl
        if hasattr(fcntl, "F_FULLFSYNC"):  # macOS: 일반 fsync 는 드라이브 캐시까지 비우지 않는다
            fcntl.fcntl(f.fileno(), fcntl.F_FULLFSYNC)
            return
    except (ImportError, OSError):
        pass
    os.fsync(f.fileno())


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        _fsync(f)
    atomic.replace(tmp, path)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _recheck_recent(dest, session, done, log, deep):
    """이어하기 시작할 때 마지막으로 기록된 파일들이 실제로 온전한지 확인한다.
    비정상 종료(전원 차단 등) 직전의 파일은 기록은 있어도 내용이 덜 써졌을 수 있다.
    이상하면 무효로 표시해서 이번에 다시 꺼낸다. 반환: 무효로 표시한 개수"""
    bad = 0
    for it in list(done.values())[-RECHECK_LAST:]:
        path = os.path.join(dest, it["path"])
        why = None
        if not os.path.exists(path):
            why = "파일이 없음"
        elif os.path.getsize(path) != it["bytes"]:
            why = "크기가 다름"
        elif deep and _sha256_file(path) != it["sha256"]:
            why = "내용이 기록과 다름"
        if why:
            bad += 1
            _rm(path)
            done.pop(it["id"], None)
            log.write(json.dumps({"id": it["id"], "invalid": True, "reason": why}) + "\n")
            log.flush()
    return bad


def extract_batch(device_path, session, plan, forbidden=(), on_progress=None, should_cancel=None):
    """다음 묶음을 꺼낸다. 반환 dict: {"state", "files", "bytes", "failed", "error"}
    state: done | cancelled | disconnected | nospace | unstable | refused | error"""
    # 1) 저장 폴더가 안전한지 먼저 본다(원본 디스크·저장소·시스템 폴더는 거부). 이 검사 전에는 아무것도 지우지 않는다
    early = check_dest(plan.get("dest"), forbidden, 0)
    if not early["ok"]:
        return {"state": "refused", "files": 0, "bytes": 0, "failed": 0, "error": " / ".join(early["reasons"])}
    dest = early["dest"]
    os.makedirs(dest, exist_ok=True)
    state_path = os.path.join(session, "run_state.json")
    prev = None
    if os.path.exists(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                prev = json.load(f).get("state")
        except ValueError:
            prev = "running"  # 상태 파일이 깨졌다면 비정상 종료로 본다
    # 2) 이전에 기록된 마지막 파일들이 온전한지 확인한다(비정상 종료 뒤에는 해시까지)
    with open(os.path.join(session, "extracted.jsonl"), "a", encoding="utf-8") as log:
        _recheck_recent(dest, session, load_extracted(session), log, deep=prev in UNCLEAN)
    # 3) 계획 점검(공간 포함)
    chk = check_plan(session, plan, forbidden)
    if not chk["ok"]:
        return {"state": "refused", "files": 0, "bytes": 0, "failed": 0, "error": " / ".join(chk["reasons"])}
    records = load_catalog(session)
    done = load_extracted(session)
    failed_ids = load_failed(session)
    dup_ids = load_dups(session)
    by_hash = {r["sha256"]: r["path"] for r in done.values() if r.get("sha256")}  # 내용 해시 -> 이미 꺼낸 파일
    got_files = got_bytes = n_failed = n_dups = consecutive = 0
    state, error = "done", None
    _write_json(os.path.join(session, "plan.json"), dict(plan, dest=dest))
    _write_json(state_path, {"state": "running", "started": time.time()})
    with ReadOnlyDevice(device_path) as dev, \
            open(os.path.join(session, "extracted.jsonl"), "a", encoding="utf-8") as log, \
            open(os.path.join(session, "failed.jsonl"), "a", encoding="utf-8") as flog, \
            open(os.path.join(session, "duplicates.jsonl"), "a", encoding="utf-8") as dlog:
        batch = next_batch(select(records, plan), set(done) | failed_ids | set(dup_ids),
                           plan.get("batch", {"unit": "gb", "size": 50}))
        total_bytes = sum(r["length"] for r in batch)
        kind_total, kind_done = {}, {}  # 화면에 "사진 1,350 / 3,214" 처럼 보여 주기 위한 종류별 개수
        for r in batch:
            k = KIND_DIR.get(r["kind"], "기타")
            kind_total[k] = kind_total.get(k, 0) + 1
            kind_done[k] = 0
        def _emit(rel, bytes_now):
            if on_progress:
                on_progress({"files": got_files, "bytes": bytes_now, "total_files": len(batch),
                             "total_bytes": total_bytes, "current": rel,
                             "by_kind": {k: [kind_done[k], kind_total[k]] for k in kind_total}})

        for rec in batch:
            if should_cancel and should_cancel():
                state = "cancelled"
                break
            rel = dest_relpath(rec)
            final = os.path.join(dest, rel)
            part = final + ".part"
            try:
                os.makedirs(os.path.dirname(final), exist_ok=True)
                if shutil.disk_usage(os.path.dirname(final)).free < rec["length"] + margin_for(rec["length"]):
                    state, error = "nospace", "저장 공간이 부족해 중단했습니다."
                    break
                h = hashlib.sha256()
                written = 0
                with open(part, "wb") as out:
                    while written < rec["length"]:
                        chunk = _read(dev, rec["offset"] + written, min(COPY_CHUNK, rec["length"] - written))
                        out.write(chunk)
                        h.update(chunk)
                        written += len(chunk)
                        _emit(rel, got_bytes + written)
                    _fsync(out)
                same = by_hash.get(h.hexdigest())
                if same and os.path.exists(os.path.join(dest, same)):
                    # 내용이 완전히 같은 파일(예: 지웠던 옛 사본)은 하나만 둔다. 삭제 여부는 알 수 없으므로 판단하지 않는다.
                    _rm(part)
                    dlog.write(json.dumps({"id": rec["id"], "path": rel, "duplicate_of": same, "bytes": rec["length"],
                                           "sha256": h.hexdigest()}, ensure_ascii=False) + "\n")
                    dlog.flush()
                    os.fsync(dlog.fileno())
                    n_dups += 1
                    kind_done[KIND_DIR.get(rec["kind"], "기타")] += 1
                    consecutive = 0
                    _emit(rel, got_bytes)
                    continue
                os.replace(part, final)
            except SourceGone:
                _rm(part)
                state, error = "disconnected", "장치 연결이 끊겼습니다. 케이블을 다시 꽂고 이어서 하세요."
                break
            except SourceUnreadable as e:
                _rm(part)  # 이 파일만 건너뛴다. 원본 디스크의 불량 구간일 수 있다
                n_failed += 1
                consecutive += 1
                flog.write(json.dumps({"id": rec["id"], "path": rel, "error": str(e)}, ensure_ascii=False) + "\n")
                flog.flush()
                if consecutive >= MAX_CONSECUTIVE_FAILS:
                    state, error = "unstable", "연속으로 %d개를 읽지 못해 중단했습니다. 디스크나 케이블 상태를 확인하세요." % consecutive
                    break
                continue
            except OSError as e:  # 저장 폴더 쪽 문제 (원본이 아니다)
                _rm(part)
                if e.errno == errno.ENOSPC:
                    state, error = "nospace", "저장 공간이 부족해 중단했습니다."
                else:
                    state, error = "error", "저장 폴더에 쓰지 못했습니다(저장 장치가 빠졌거나 권한 문제일 수 있습니다): %s" % e
                break
            consecutive = 0
            log.write(json.dumps({"id": rec["id"], "path": rel, "bytes": rec["length"],
                                  "sha256": h.hexdigest(), "kind": rec["kind"]}, ensure_ascii=False) + "\n")
            log.flush()
            os.fsync(log.fileno())
            by_hash[h.hexdigest()] = rel
            kind_done[KIND_DIR.get(rec["kind"], "기타")] += 1
            got_files += 1
            got_bytes += rec["length"]
            _emit(rel, got_bytes)  # 파일이 끝난 직후의 개수를 보여 준다(마지막 파일이 "27 / 28" 로 멈춰 보이지 않게)
    _write_json(state_path, {"state": state, "finished": time.time()})
    return {"state": state, "files": got_files, "bytes": got_bytes, "failed": n_failed, "duplicates": n_dups,
            "error": error}


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass
