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
