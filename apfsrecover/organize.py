"""꺼낸 파일을 '정리해서 복사': 촬영 시각 이름 · 연도/월/날짜 폴더 · 종류 합치기.

꺼낸 폴더(저장 폴더)는 읽기만 하고, 새 폴더에 복사본만 만든다. 원본 장치는 읽지 않는다.
복사가 끝나면 복사본을 다시 읽어, 꺼낼 때 남긴 지문(sha256)과 맞는지 확인한다.

options:
  rename  "time"(촬영 시각+카메라) | "orig"(원래 이름)
  layout  "ymd"(연도/월/날짜) | "day"(날짜만)
  merge   True: 날짜 폴더 하나에 사진·RAW·영상(여러 종류인 날만 종류 폴더) / False: 종류 폴더 아래 날짜
  review  True: 날짜불명 · 100KB 미만 사진 · _의심 은 _확인용 으로
  groups  ["photo", "raw", "video", "docs"] 중 고른 것
"""
import csv
import hashlib
import os
import re
import shutil
import time
from collections import Counter, defaultdict

from . import extract as ex
from .exif import parse_tiff, to_iso
from .timeutil import to_local

GROUP_OF = {"사진": "photo", "RAW": "raw", "영상": "video"}
GROUP_DIR = {"photo": "사진", "raw": "RAW", "video": "영상"}
REVIEW = "_확인용"
DOCS = "문서·압축"
LIST_NAME = "_정리 목록.csv"
SMALL = 100 * 1024
DEFAULTS = {"rename": "time", "layout": "ymd", "merge": True, "review": True, "groups": ["photo", "raw", "video"]}
# 기록에 없는 파일(사람이 압축을 풀었거나 넣은 것)의 종류
EXT_GROUP = {".jpg": "photo", ".jpeg": "photo", ".heic": "photo", ".heif": "photo", ".png": "photo", ".gif": "photo", ".webp": "photo",
             ".cr3": "raw", ".cr2": "raw", ".raf": "raw", ".arw": "raw", ".nef": "raw", ".dng": "raw", ".orf": "raw", ".rw2": "raw",
             ".mov": "video", ".mp4": "video", ".m4v": "video", ".avi": "video", ".3gp": "video"}
SKIP_DIRS = {"_원래이름"}            # 원래 이름 되살리기가 만든 하드 링크(꺼낸 파일과 같은 파일)
SKIP_TOP_FILES = {"복구 보고서.txt"}
NAME_DT = re.compile(r"(?<!\d)(20\d\d)[-_.]?(\d\d)[-_.]?(\d\d)[ _-]?(\d\d)[-_.:]?(\d\d)[-_.:]?(\d\d)(?!\d)")


def _group(rec):
    if rec.get("group"):
        return rec["group"]
    return GROUP_OF.get(ex.KIND_DIR.get(rec["kind"]), "docs")


def _short_model(make, model):
    if not model:
        return ""
    m = model.strip()
    if make and m.lower().startswith(make.strip().lower()):
        m = m[len(make.strip()):].strip()
    return re.sub(r'[\\/:*?"<>|]', "_", m)


def opts_of(o):
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if o and k in o:
            out[k] = o[k]
    out["groups"] = [g for g in ("photo", "raw", "video", "docs") if g in (out["groups"] or [])]
    return out


def _walk(top):
    """{소문자 경로: (경로, 크기)}. 윈도우는 폴더를 읽을 때 크기도 함께 와서 빠르다."""
    out, stack = {}, [top]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        out[e.path.lower()] = (e.path, e.stat(follow_symlinks=False).st_size)
                except OSError:
                    pass
    return out


def _guess_meta(path, group):
    """기록에 없는 파일의 촬영 시각·카메라: JPEG 는 EXIF, 아니면 파일 이름의 날짜(20160711_213705 등)."""
    meta = {}
    if group == "photo" and path.lower().endswith((".jpg", ".jpeg")):
        try:
            with open(path, "rb") as f:
                head = f.read(256 * 1024)
            k = head.find(b"Exif\0\0")
            if k != -1:
                meta = {kk: v for kk, v in parse_tiff(head[k + 6:]).items() if kk in ("taken", "make", "model")}
        except OSError:
            pass
    if not meta.get("taken"):
        m = NAME_DT.search(os.path.basename(path))
        if m:
            meta["taken"] = to_iso(("%s:%s:%s %s:%s:%s" % m.groups()).encode())
    return meta


def collect(pairs):
    """pairs: [(기록 폴더, 그 기록의 저장 폴더)].
    반환 [(경로, 기록, 상대 경로, 크기)]: 저장 폴더에 실제로 있는 파일만. 기록에 없는 파일(사람이 넣은 것)도 extra 로 넣는다."""
    items, seen, files = [], set(), {}
    for s, src in pairs:
        if not src:
            continue
        if src.lower() not in files:
            files[src.lower()] = (src, _walk(src))
        have = files[src.lower()][1]
        if not os.path.isfile(os.path.join(s, "catalog.jsonl")):
            continue
        cat = {r["id"]: r for r in ex.load_catalog(s)}
        for i, e in ex.load_extracted(s).items():
            r = cat.get(i)
            p = os.path.join(src, e.get("path", ""))
            hit = have.get(p.lower())
            if not r or not hit or p.lower() in seen:
                continue
            seen.add(p.lower())
            items.append((hit[0], dict(r, sha256=e.get("sha256")), e["path"].replace("/", "\\"), hit[1]))
    for src, have in files.values():
        for low, (p, size) in sorted(have.items()):
            if low in seen or low.endswith(".복사중"):
                continue
            rel = os.path.relpath(p, src)
            parts = rel.split(os.sep)
            if any(x in SKIP_DIRS for x in parts) or (len(parts) == 1 and (parts[0] in SKIP_TOP_FILES or parts[0].lower().endswith(".csv"))):
                continue
            seen.add(low)
            g = EXT_GROUP.get(os.path.splitext(p)[1].lower(), "docs")
            rec = {"kind": "extra", "group": g, "extra": True}
            if g != "docs":
                rec.update(_guess_meta(p, g))
            items.append((p, rec, rel, size))
    return items


def plan(items, out, opts):
    """반환: (목록 [(원본, 복사할 곳, 크기, 지문)], 개수 Counter). 기록에 있는 것을 먼저 이름 붙여, 나중에 파일이 늘어도 이름이 바뀌지 않게 한다."""
    o = opts_of(opts)
    keep = [x for x in items if _group(x[1]) in o["groups"]]
    days = defaultdict(set)
    for p, r, rel, size in keep:
        dt = to_local(r.get("taken"), r.get("taken_utc", False))
        if dt and ex.SUSPECT_DIR not in rel.split("\\") and _group(r) != "docs":
            days[dt.strftime("%Y-%m-%d")].add(_group(r))
    res, used, cnt = [], set(), Counter()
    for p, r, rel, size in sorted(keep, key=lambda x: (bool(x[1].get("extra")), x[1].get("taken") or "", x[0])):
        g = _group(r)
        ext = os.path.splitext(p)[1].lower()
        if g == "docs":
            tgt = os.path.join(out, DOCS, *rel.split("\\"))
            cnt["docs"] += 1
        else:
            dt = to_local(r.get("taken"), r.get("taken_utc", False))
            kd = GROUP_DIR[g]
            name = None
            if dt and o["rename"] == "time":
                mdl = _short_model(r.get("make"), r.get("model"))
                name = dt.strftime("%Y-%m-%d_%H%M%S") + ("_" + mdl if mdl else "")
            base = name or os.path.splitext(os.path.basename(p))[0]
            if o["review"] and ex.SUSPECT_DIR in rel.split("\\"):
                d, why = os.path.join(out, REVIEW, "깨졌을 수 있음", kd), "suspect"
            elif o["review"] and not dt:
                d, why = os.path.join(out, REVIEW, "날짜불명", kd), "undated"
            elif o["review"] and g == "photo" and size < SMALL:
                d, why = os.path.join(out, REVIEW, "작은 사진"), "small"
            else:
                day = dt.strftime("%Y-%m-%d") if dt else ex.UNDATED
                parts = [day[:4], day[:7], day] if (dt and o["layout"] == "ymd") else [day]
                if o["merge"]:
                    d = os.path.join(out, *parts)
                    if len(days.get(day, ())) > 1:
                        d = os.path.join(d, kd)
                else:
                    d = os.path.join(out, kd, *parts)
                why = g
            tgt = os.path.join(d, base + ext)
            cnt[why] += 1
        if r.get("extra"):
            cnt["extra"] += 1
        stem, ext2 = os.path.splitext(tgt)
        k = 2
        while tgt.lower() in used:
            tgt, k = "%s_%d%s" % (stem, k, ext2), k + 1
        used.add(tgt.lower())
        res.append((p, tgt, size, r.get("sha256")))
    return res, cnt


def samples(rows, out, n=4):
    """미리보기: 종류가 다른 것 위주로 몇 개."""
    pick, seen = [], set()
    for a, b, _, _ in rows:
        top = os.path.relpath(b, out).split(os.sep)
        key = (top[0] == REVIEW, os.path.splitext(b)[1], len(top))
        if key not in seen:
            seen.add(key)
            pick.append((os.path.basename(a), os.path.relpath(b, out)))
        if len(pick) >= n:
            break
    return pick


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(8 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def run(rows, out, on_progress=None, should_cancel=None):
    """복사(이미 같은 크기로 있으면 건너뜀) → 복사본 다시 읽어 지문 확인. 반환: 결과 dict."""
    should_cancel = should_cancel or (lambda: False)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, LIST_NAME), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["원래 파일", "정리된 파일"])
        w.writerows((a, b) for a, b, _, _ in rows)
    total = sum(r[2] for r in rows)
    res = {"files": len(rows), "bytes": total, "copied": 0, "skipped": 0, "checked": 0, "mismatch": [], "finished": False, "dir": out}
    done, last = 0, 0.0

    def tick(phase, n, force=False):
        nonlocal last
        if on_progress and (force or time.time() - last > 0.5):
            last = time.time()
            on_progress({"phase": phase, "done": n, "total": len(rows), "done_bytes": done, "total_bytes": total})
    there = _walk(out)
    for n, (a, b, size, _) in enumerate(rows):
        if should_cancel():
            return res
        if (there.get(b.lower()) or (None, -1))[1] == size:
            res["skipped"] += 1
        else:
            os.makedirs(os.path.dirname(b), exist_ok=True)
            tmp = b + ".복사중"
            shutil.copyfile(a, tmp)
            shutil.copystat(a, tmp)
            os.replace(tmp, b)
            res["copied"] += 1
        done += size
        tick("copy", n + 1)
    tick("copy", len(rows), True)
    done = 0
    for n, (a, b, size, sha) in enumerate(rows):
        if should_cancel():
            return res
        if (sha or _sha(a)) != _sha(b):
            res["mismatch"].append(b)
        res["checked"] += 1
        done += size
        tick("verify", n + 1)
    res["finished"] = True
    return res


def present_bytes(rows, out):
    """이미 같은 크기로 복사돼 있는 양(이어서 하기·공간 계산용)."""
    if not os.path.isdir(out):
        return 0
    there = _walk(out)
    return sum(size for a, b, size, _ in rows if (there.get(b.lower()) or (None, -1))[1] == size)
