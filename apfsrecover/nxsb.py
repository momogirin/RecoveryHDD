"""APFS 컨테이너 슈퍼블록(nx_superblock_t) 파싱과 체크포인트 영역 스캔.

필드 오프셋은 Apple File System Reference 기준이며, 체크섬(Fletcher-64)이
맞는 블록에서만 신뢰한다.
"""
import struct

NX_MAGIC = 0x4253584E  # 'NXSB' (little-endian)
OBJ_TYPE_MASK = 0x0000FFFF
OBJECT_TYPES = {
    0x1: "NX_SUPERBLOCK",
    0x2: "BTREE",
    0x3: "BTREE_NODE",
    0x5: "SPACEMAN",
    0xB: "OMAP",
    0xC: "CHECKPOINT_MAP",
    0xD: "FS(volume superblock)",
}
# Apple 문서: OBJ_VIRTUAL=0, OBJ_EPHEMERAL=0x80000000, OBJ_PHYSICAL=0x40000000
OBJ_STORAGE = {0x80000000: "EPHEMERAL", 0x40000000: "PHYSICAL", 0x00000000: "VIRTUAL"}
M = 0xFFFFFFFF


def fletcher64_ok(block: bytes) -> bool:
    """APFS 오브젝트 체크섬 검증 (처음 8바이트 제외한 32비트 LE 워드 대상)."""
    if len(block) % 4 or len(block) < 16:
        return False
    words = struct.unpack("<%dI" % ((len(block) - 8) // 4), block[8:])
    s1 = s2 = 0
    for w in words:
        s1 = (s1 + w) % M
        s2 = (s2 + s1) % M
    c1 = M - ((s1 + s2) % M)
    c2 = M - ((s1 + c1) % M)
    return struct.unpack("<Q", block[:8])[0] == (c2 << 32) | c1


def obj_header(block: bytes):
    oid, xid, typ, sub = struct.unpack_from("<QQII", block, 8)
    return {"oid": oid, "xid": xid, "type": typ, "subtype": sub}


def parse_nxsb(block: bytes) -> dict:
    h = obj_header(block)
    (magic, bsize, bcount, feat, ro_feat, incompat) = struct.unpack_from("<IIQQQQ", block, 32)
    uuid = block[72:88].hex()
    next_oid, next_xid = struct.unpack_from("<QQ", block, 88)
    (desc_blocks, data_blocks) = struct.unpack_from("<II", block, 104)
    (desc_base, data_base) = struct.unpack_from("<qq", block, 112)
    (desc_next, data_next, desc_index, desc_len, data_index, data_len) = struct.unpack_from("<6I", block, 128)
    spaceman, omap, reaper = struct.unpack_from("<QQQ", block, 152)
    max_fs = struct.unpack_from("<I", block, 180)[0]
    fs_oids = [o for o in struct.unpack_from("<100Q", block, 184) if o]
    flags = struct.unpack_from("<Q", block, 1264)[0]
    kl_start, kl_count = struct.unpack_from("<QQ", block, 1296)
    return {
        **h, "magic": magic, "block_size": bsize, "block_count": bcount,
        "features": feat, "ro_compat": ro_feat, "incompat": incompat, "uuid": uuid,
        "next_oid": next_oid, "next_xid": next_xid,
        "xp_desc_blocks": desc_blocks, "xp_data_blocks": data_blocks,
        "xp_desc_base": desc_base, "xp_data_base": data_base,
        "xp_desc_next": desc_next, "xp_data_next": data_next,
        "xp_desc_index": desc_index, "xp_desc_len": desc_len,
        "xp_data_index": data_index, "xp_data_len": data_len,
        "spaceman_oid": spaceman, "omap_oid": omap, "reaper_oid": reaper,
        "max_file_systems": max_fs, "fs_oids": fs_oids, "flags": flags,
        "keylocker_start": kl_start, "keylocker_blocks": kl_count,
    }


def classify(block: bytes):
    """체크포인트 영역의 블록 하나를 분류한다."""
    ok = fletcher64_ok(block)
    h = obj_header(block)
    base = h["type"] & OBJ_TYPE_MASK
    storage = OBJ_STORAGE.get(h["type"] & 0xC0000000, "?")
    return {
        "checksum_ok": ok, **h, "base_type": base,
        "type_name": OBJECT_TYPES.get(base, "0x%x" % base), "storage": storage,
        "is_nxsb": base == 0x1 and struct.unpack_from("<I", block, 32)[0] == NX_MAGIC,
    }
