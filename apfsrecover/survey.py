"""디스크 전체를 듬성듬성 샘플링해서 데이터 분포 지도를 만든다 (읽기 전용).

step 바이트마다 4KB 하나를 읽어 '비어 있음(전부 0)', '데이터', JPEG/MP4 시그니처
흔적을 센다. 구간(bucket) 별 요약만 출력하고 파일은 쓰지 않는다.
"""
import sys
import time

from .blockdev import ReadOnlyDevice

BS = 4096


def survey(path: str, total_bytes: int, step_mb: int, bucket_gb: int) -> int:
    step = step_mb * 1024 * 1024
    bucket = bucket_gb * 1000 * 1000 * 1000
    buckets = {}
    t0 = time.time()
    n = 0
    with ReadOnlyDevice(path) as dev:
        for off in range(0, total_bytes - BS, step):
            blk = dev.read_at(off, BS)
            if len(blk) < BS:
                continue
            b = buckets.setdefault(off // bucket, {"samples": 0, "data": 0, "jpeg": 0, "ftyp": 0})
            b["samples"] += 1
            if any(blk):
                b["data"] += 1
            if blk[:3] == b"\xff\xd8\xff":
                b["jpeg"] += 1
            if blk[4:8] == b"ftyp":
                b["ftyp"] += 1
            n += 1
            if n % 2000 == 0:
                print("\r%.0f%% (%d 샘플, %.0fs)" % (off * 100 / total_bytes, n, time.time() - t0),
                      end="", file=sys.stderr, flush=True)
    print("\n구간(GB)        샘플  데이터있음  JPEG시작  MP4ftyp")
    for k in sorted(buckets):
        b = buckets[k]
        print("%5d-%-5d  %7d  %8.0f%%  %8d  %7d" % (
            k * bucket_gb, (k + 1) * bucket_gb, b["samples"],
            100.0 * b["data"] / b["samples"], b["jpeg"], b["ftyp"]))
    return 0


def estimate_extent(dev, total_bytes, step, on_progress=None, should_cancel=None):
    """step 바이트 간격으로 4KB 를 읽어 데이터가 있는 가장 끝 위치를 추정한다(읽기 전용).

    샘플링이라 듬성듬성한 뒤쪽 데이터는 놓칠 수 있다. 호출하는 쪽이 여유(margin)를 더하고,
    사용자가 '전체 검사'를 고를 수 있게 해야 한다.
    반환: {"last_data_offset": int|None, "samples": n, "nonzero": n, "cancelled": bool}
    """
    last, nonzero, n = None, 0, 0
    for off in range(0, max(0, total_bytes - BS), step):
        if should_cancel and should_cancel():
            return {"last_data_offset": last, "samples": n, "nonzero": nonzero, "cancelled": True}
        blk = dev.read_at(off, BS)
        n += 1
        if any(blk):
            nonzero += 1
            last = off
        if on_progress and n % 200 == 0:
            on_progress({"pos": off, "total": total_bytes})
    return {"last_data_offset": last, "samples": n, "nonzero": nonzero, "cancelled": False}
