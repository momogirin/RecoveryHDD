"""GPT 파티션 표 해석(읽기 전용). 디스크 전체를 읽는 Windows 에서 파티션 시작 위치를 직접 구한다.

OS 가 알려 주는 파티션 정보를 쓸 수 없을 때(권한, 알 수 없는 형식)의 대비책이다.
헤더/항목 CRC 가 맞지 않으면 추측하지 않고 None 을 돌려준다.
"""
import struct
import uuid
import zlib

KNOWN = {
    "C12A7328-F81F-11D2-BA4B-00A0C93EC93B": "EFI",
    "E3C9E316-0B5C-4DB8-817D-F92DF00215AE": "Microsoft_Reserved",
    "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7": "Microsoft_Basic_Data",
    "7C3457EF-0000-11AA-AA11-00306543ECAC": "Apple_APFS",
    "48465300-0000-11AA-AA11-00306543ECAC": "Apple_HFS",
    "426F6F74-0000-11AA-AA11-00306543ECAC": "Apple_Boot",
    "53746F72-6167-11AA-AA11-00306543ECAC": "Apple_CoreStorage",
}
MAX_ENTRIES = 1024


def parse_gpt(read_at, sector_sizes=(512, 4096)):
    """read_at(offset, length) -> bytes. 반환: {"sector_size", "partitions":[...]} 또는 None."""
    for ss in sector_sizes:
        hdr = read_at(ss, 92)
        if len(hdr) < 92 or hdr[:8] != b"EFI PART":
            continue
        hsize, = struct.unpack_from("<I", hdr, 12)
        if not 92 <= hsize <= ss:
            continue
        full = read_at(ss, hsize)
        crc, = struct.unpack_from("<I", full, 16)
        if zlib.crc32(full[:16] + b"\0\0\0\0" + full[20:]) & 0xFFFFFFFF != crc:
            return None
        entry_lba, n, esize, ecrc = struct.unpack_from("<QIII", full, 72)
        if not (0 < n <= MAX_ENTRIES and esize >= 128 and esize % 8 == 0 and esize <= 1024):
            return None
        table = read_at(entry_lba * ss, n * esize)
        if len(table) < n * esize or zlib.crc32(table) & 0xFFFFFFFF != ecrc:
            return None
        parts = []
        for i in range(n):
            e = table[i * esize:(i + 1) * esize]
            type_guid = uuid.UUID(bytes_le=e[:16])
            if type_guid.int == 0:
                continue
            first, last = struct.unpack_from("<QQ", e, 32)
            if last < first:
                return None
            tg = str(type_guid).upper()
            parts.append({"number": i + 1, "type_guid": tg, "content": KNOWN.get(tg, "Unknown"),
                          "offset": first * ss, "size": (last - first + 1) * ss,
                          "name": e[56:128].decode("utf-16-le", "replace").split("\0")[0]})
        return {"sector_size": ss, "partitions": parts}
    return None
