"""P5: 꺼낸 파일이 열리는지 검증한다 (저장 폴더의 파일만 읽는다. 원본 디스크는 건드리지 않는다).

검증 수준(level)을 결과에 같이 기록해서, 무엇까지 확인했는지 숨기지 않는다.
  structure : 파일 구조가 처음부터 끝까지 맞는지(JPEG 마커/EOI, MP4·CR3 박스 합계=파일 크기)
  decode    : 이미지를 끝까지 디코딩해 봄 (Pillow 가 있을 때, 또는 macOS sips)
  probe     : ffprobe 로 영상 메타데이터를 읽어 봄 (ffprobe 가 있을 때)
재생·현상 프로그램에서의 열림은 확인하지 않는다.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from collections import Counter

from .extract import load_catalog, load_extracted
from .formats import BMFF_TYPES, jpeg_parse

MAX_WHOLE_READ = 256 << 20


def available_tools():
    tools = {"pillow": False, "sips": False, "ffprobe": False}
    try:
        import PIL  # noqa: F401
        tools["pillow"] = True
    except ImportError:
        pass
    tools["sips"] = sys.platform == "darwin" and shutil.which("sips") is not None
    tools["ffprobe"] = shutil.which("ffprobe") is not None
    return tools


def _bmff_walk(path, size):
    """파일 전체를 최상위 박스로 정확히 덮는지 확인. 반환: (ok, seen, 이유)"""
    seen = set()
    pos = 0
    with open(path, "rb") as f:
        while pos < size:
            f.seek(pos)
            h = f.read(16)
            if len(h) < 8:
                return False, seen, "박스 헤더가 잘림"
            sz, typ = struct.unpack_from(">I4s", h, 0)
            if typ not in BMFF_TYPES:
                return False, seen, "알 수 없는 박스 %r" % typ
            if sz == 1:
                if len(h) < 16:
                    return False, seen, "박스 헤더가 잘림"
                sz = struct.unpack_from(">Q", h, 8)[0]
            if sz < 8:
                return False, seen, "박스 크기 이상"
            seen.add(typ)
            pos += sz
    if pos != size:
        return False, seen, "박스 합계(%d)와 파일 크기(%d)가 다름 → 잘렸거나 길이가 틀림" % (pos, size)
    return True, seen, ""


def _decode_image(path, tools):
    """디코딩 확인. 반환: (ok, level, 이유) — 도구가 없으면 (None, None, "")"""
    if tools["pillow"]:
        try:
            from PIL import Image
            with Image.open(path) as im:
                im.load()
            return True, "decode", ""
        except Exception as e:  # Pillow 는 다양한 예외를 낸다
            return False, "decode", "디코딩 실패: %s" % e
    if tools["sips"]:
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(["sips", "-s", "format", "png", path, "--out", os.path.join(td, "o.png")],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0 or "Error" in (r.stderr + r.stdout):
                return False, "decode", "디코딩 실패(sips): %s" % (r.stderr or r.stdout).strip()[:120]
        return True, "decode", ""
    return None, None, ""


def verify_file(path, kind, expected_size, tools, deep=True):
    """반환: {"status": ok|fail, "level": ..., "reason": ...}"""
    if not os.path.exists(path):
        return {"status": "fail", "level": "-", "reason": "파일이 없음"}
    size = os.path.getsize(path)
    if size != expected_size:
        return {"status": "fail", "level": "-", "reason": "크기 불일치(%d != %d)" % (size, expected_size)}
    if kind == "jpeg":
        if size > MAX_WHOLE_READ:
            return {"status": "fail", "level": "structure", "reason": "JPEG 가 비정상적으로 큼"}
        with open(path, "rb") as f:
            data = f.read()
        status, length, _ = jpeg_parse(data)
        if status != "ok":
            return {"status": "fail", "level": "structure",
                    "reason": "잘림(EOI 없음)" if status == "short" else "구조 오류"}
        if length != size:
            return {"status": "fail", "level": "structure", "reason": "EOI 위치와 파일 크기가 다름"}
        if deep:
            ok, level, why = _decode_image(path, tools)
            if ok is False:
                return {"status": "fail", "level": level, "reason": why}
            if ok:
                return {"status": "ok", "level": level, "reason": ""}
        return {"status": "ok", "level": "structure", "reason": ""}
    # BMFF 계열
    ok, seen, why = _bmff_walk(path, size)
    if not ok:
        return {"status": "fail", "level": "structure", "reason": why}
    need = {b"meta", b"mdat"} if kind == "heic" else {b"moov", b"mdat"}
    if not need <= seen:
        return {"status": "fail", "level": "structure", "reason": "필수 박스 없음"}
    if deep and kind in ("video-mp4", "video-mov") and tools["ffprobe"]:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or r.stderr.strip():
            return {"status": "fail", "level": "probe", "reason": "ffprobe 오류: %s" % r.stderr.strip()[:120]}
        return {"status": "ok", "level": "probe", "reason": ""}
    return {"status": "ok", "level": "structure", "reason": ""}


def verify_session(session, dest, on_progress=None, should_cancel=None, deep=True):
    """extracted.jsonl 의 모든 파일을 검증하고 verify.jsonl 을 쓴다. 반환: 요약 dict"""
    tools = available_tools()
    kinds = {r["id"]: r["kind"] for r in load_catalog(session)}
    items = sorted(load_extracted(session).values(), key=lambda r: r["id"])
    out_path = os.path.join(session, "verify.jsonl")
    counts, levels, reasons = Counter(), Counter(), Counter()
    cancelled = False
    with open(out_path, "w", encoding="utf-8") as out:
        for i, it in enumerate(items):
            if should_cancel and should_cancel():
                cancelled = True
                break
            res = verify_file(os.path.join(dest, it["path"]), it["kind"] or kinds.get(it["id"]),
                              it["bytes"], tools, deep)
            res.update(id=it["id"], path=it["path"])
            out.write(json.dumps(res, ensure_ascii=False) + "\n")
            counts[res["status"]] += 1
            levels[res["level"]] += 1
            if res["status"] == "fail":
                reasons[res["reason"]] += 1
            if on_progress:
                on_progress({"done": i + 1, "total": len(items), "ok": counts["ok"], "fail": counts["fail"]})
    summary = {"total": len(items), "ok": counts["ok"], "fail": counts["fail"],
               "unchecked": len(items) - counts["ok"] - counts["fail"], "levels": dict(levels),
               "reasons": dict(reasons), "tools": tools, "cancelled": cancelled}
    with open(os.path.join(session, "verify_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    return summary


def load_summary(session):
    p = os.path.join(session, "verify_summary.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_failures(session, limit=200):
    p = os.path.join(session, "verify.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for l in f:
            r = json.loads(l)
            if r["status"] == "fail":
                out.append(r)
                if len(out) >= limit:
                    break
    return out
