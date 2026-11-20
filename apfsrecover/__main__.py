"""사용법 (모두 읽기 전용):
  python3 -m apfsrecover inspect /dev/rdisk4s2
  python3 -m apfsrecover scan /dev/rdisk4s2 <시작블록> <끝블록> <출력.jsonl>
  python3 -m apfsrecover survey /dev/rdisk4s2 <총바이트> <샘플간격MB> <구간GB>
  python3 -m apfsrecover catalog <장치> <세션폴더> <시작블록> <끝블록>   (파일 목록 만들기, 이어하기 지원)
  python3 -m apfsrecover summary <세션폴더>                              (목록 요약)"""
import sys

from .blockdev import ReadOnlyDevice
from .nxsb import classify, fletcher64_ok, parse_nxsb

SECTOR = 512


def hexdump(b: bytes, n: int = 64) -> str:
    return " ".join("%02x" % x for x in b[:n])


def inspect(path: str) -> int:
    with ReadOnlyDevice(path) as dev:
        raw0 = dev.read_at(0, 4096)
        if len(raw0) < 4096:
            print("블록 0 을 읽지 못함 (%d 바이트)" % len(raw0))
            return 1
        print("== 블록 0 (처음 64바이트 원본) ==\n", hexdump(raw0), sep="")
        sb = parse_nxsb(raw0)
        bsize = sb["block_size"]
        print("매직 0x%08x (NXSB=0x4253584e), block_size=%d" % (sb["magic"], bsize))
        if sb["magic"] != 0x4253584E or bsize not in (4096, 8192, 16384, 32768, 65536):
            print("!! 블록 0 이 유효한 NXSB 가 아님 — 추측하지 않고 중단")
            return 2
        raw = dev.read_at(0, bsize)
        print("블록 0 체크섬:", "OK" if fletcher64_ok(raw) else "불일치(손상)")
        for k in ("oid", "xid", "block_count", "uuid", "next_oid", "next_xid",
                  "xp_desc_blocks", "xp_desc_base", "xp_desc_index", "xp_desc_len",
                  "xp_data_blocks", "xp_data_base", "xp_data_index", "xp_data_len",
                  "omap_oid", "spaceman_oid", "reaper_oid", "max_file_systems"):
            print("  %-18s %s" % (k, sb[k]))
        print("  fs_oids            ", sb["fs_oids"])
        print("  incompat=0x%x flags=0x%x (NX_CRYPTO_SW=0x4)" % (sb["incompat"], sb["flags"]))
        print("  keylocker start=%d blocks=%d" % (sb["keylocker_start"], sb["keylocker_blocks"]))
        print("  용량 %.2f TB (블록수*블록크기)" % (sb["block_count"] * bsize / 1e12))

        base, n = sb["xp_desc_base"], sb["xp_desc_blocks"]
        if base < 0:
            print("!! 체크포인트 디스크립터가 비연속(트리) 형태 — 아직 미지원")
            return 3
        print("\n== 체크포인트 디스크립터 영역: 블록 %d..%d ==" % (base, base + n - 1))
        area = dev.read_at(base * bsize, n * bsize)
        rows = []
        for i in range(n):
            blk = area[i * bsize:(i + 1) * bsize]
            if len(blk) < bsize or not any(blk[:64]):
                continue
            c = classify(blk)
            rows.append((i, c))
        print("%5s %-10s %-22s %-9s %10s %6s" % ("idx", "체크섬", "type", "storage", "xid", "NXSB"))
        for i, c in rows:
            print("%5d %-10s %-22s %-9s %10d %6s" % (
                i, "OK" if c["checksum_ok"] else "BAD", c["type_name"], c["storage"],
                c["xid"], "yes" if c["is_nxsb"] else ""))
        good = sorted({c["xid"] for _, c in rows if c["is_nxsb"] and c["checksum_ok"]})
        print("\n체크섬이 맞는 NXSB 의 xid:", good if good else "없음")
        print("블록 0 xid:", sb["xid"])
    return 0


def main(argv):
    if len(argv) == 3 and argv[1] == "inspect":
        return inspect(argv[2])
    if len(argv) == 6 and argv[1] == "catalog":
        from .catalog import catalog
        return catalog(argv[2], argv[3], int(argv[4]), int(argv[5]))
    if len(argv) == 3 and argv[1] == "summary":
        from .catalog import print_summary
        return print_summary(argv[2])
    if len(argv) == 8 and argv[1] == "carve-sample":
        from .carve import carve_sample
        return carve_sample(argv[2], int(argv[3]), int(argv[4]), argv[5], int(argv[6]), int(argv[7]))
    if len(argv) == 4 and argv[1] == "auto":
        from .auto import auto
        return auto(argv[2], argv[3])
    if len(argv) == 6 and argv[1] == "survey":
        from .survey import survey
        return survey(argv[2], int(argv[3]), int(argv[4]), int(argv[5]))
    if len(argv) == 6 and argv[1] == "scan":
        from .scan import scan
        return scan(argv[2], int(argv[3]), int(argv[4]), argv[5])
    print(__doc__)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))
