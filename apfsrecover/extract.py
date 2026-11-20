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

from .blockdev import ReadOnlyDevice
from .guards import check_dest, margin_for
from .scan import BS
from .timeutil import local_date

KIND_EXT = {"jpeg": "jpg", "raw-cr3": "cr3", "video-mp4": "mp4", "video-mov": "mov",
            "heic": "heic", "unknown-bmff": "bin"}
COPY_CHUNK = 8 * 1024 * 1024
UNDATED = "날짜불명"


def load_catalog(session):
    with open(os.path.join(session, "catalog.jsonl"), encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def load_extracted(session):
    path = os.path.join(session, "extracted.jsonl")
    if not os.path.exists(path):
        return {}
    done = {}
    with open(path, encoding="utf-8") as f:
        for l in f:
            if l.strip():
                r = json.loads(l)
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
    return os.path.join(day, "%s_%06d_blk%d.%s" % (ext, rec["id"], rec["offset"] // BS, ext))


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


def overview(records, plan, done):
    sel = select(records, plan)
    pending = [r for r in sel if r["id"] not in done]
    nb = next_batch(sel, set(done), plan.get("batch", {"unit": "gb", "size": 50}))
    batches = 0
    rest = list(sel)
    seen = set(done)
    while True:
        b = next_batch(rest, seen, plan.get("batch", {"unit": "gb", "size": 50}))
        if not b:
            break
        seen |= {r["id"] for r in b}
        batches += 1
    return {"files": len(sel), "bytes": sum(r["length"] for r in sel),
            "done_files": len(sel) - len(pending), "pending_files": len(pending),
            "pending_bytes": sum(r["length"] for r in pending),
            "next_files": len(nb), "next_bytes": sum(r["length"] for r in nb),
            "batches_remaining": batches}


def check_plan(session, plan, forbidden=()):
    records = load_catalog(session)
    done = load_extracted(session)
    ov = overview(records, plan, done)
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


def extract_batch(device_path, session, plan, forbidden=(), on_progress=None, should_cancel=None):
    """다음 묶음을 꺼낸다. 반환 dict: {"state", "files", "bytes", "error"}
    state: done | cancelled | disconnected | nospace | refused | error"""
    chk = check_plan(session, plan, forbidden)
    if not chk["ok"]:
        return {"state": "refused", "files": 0, "bytes": 0, "error": " / ".join(chk["reasons"])}
    dest = chk["dest"]
    os.makedirs(dest, exist_ok=True)
    records = load_catalog(session)
    done = load_extracted(session)
    batch = next_batch(select(records, plan), set(done), plan.get("batch", {"unit": "gb", "size": 50}))
    total_bytes = sum(r["length"] for r in batch)
    got_files = got_bytes = 0
    state, error = "done", None
    with open(os.path.join(session, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(dict(plan, dest=dest), f, ensure_ascii=False, indent=1)
    with ReadOnlyDevice(device_path) as dev, \
            open(os.path.join(session, "extracted.jsonl"), "a", encoding="utf-8") as log:
        for rec in batch:
            if should_cancel and should_cancel():
                state = "cancelled"
                break
            rel = dest_relpath(rec)
            final = os.path.join(dest, rel)
            part = final + ".part"
            os.makedirs(os.path.dirname(final), exist_ok=True)
            if shutil.disk_usage(os.path.dirname(final)).free < rec["length"] + margin_for(rec["length"]):
                state, error = "nospace", "저장 공간이 부족해 중단했습니다."
                break
            try:
                h = hashlib.sha256()
                written = 0
                with open(part, "wb") as out:
                    while written < rec["length"]:
                        chunk = dev.read_at(rec["offset"] + written, min(COPY_CHUNK, rec["length"] - written))
                        if not chunk:
                            raise EOFError("짧게 읽힘: %d/%d" % (written, rec["length"]))
                        out.write(chunk)
                        h.update(chunk)
                        written += len(chunk)
                        if on_progress:
                            on_progress({"files": got_files, "bytes": got_bytes + written,
                                         "total_files": len(batch), "total_bytes": total_bytes,
                                         "current": rel})
                os.replace(part, final)
            except OSError as e:
                _rm(part)
                if e.errno == errno.ENOSPC:
                    state, error = "nospace", "저장 공간이 부족해 중단했습니다."
                elif e.errno in (errno.ENXIO, errno.ENODEV, errno.EIO):
                    state, error = "disconnected", "장치 연결이 끊겼습니다. 케이블을 다시 꽂고 이어서 하세요."
                else:
                    state, error = "error", "%s: %s" % (rel, e)
                break
            except EOFError as e:
                _rm(part)
                state, error = "disconnected", "%s: %s" % (rel, e)
                break
            log.write(json.dumps({"id": rec["id"], "path": rel, "bytes": rec["length"],
                                  "sha256": h.hexdigest(), "kind": rec["kind"]}, ensure_ascii=False) + "\n")
            log.flush()
            got_files += 1
            got_bytes += rec["length"]
    return {"state": state, "files": got_files, "bytes": got_bytes, "error": error}


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass
