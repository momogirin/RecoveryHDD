"""시험용 작은 APFS 컨테이너 만들기(구조만, Apple File System Reference 의 배치를 따른다).

블록 0: 컨테이너 슈퍼블록(NXSB) · 1: 체크포인트 사본 · 2: 컨테이너 오브젝트 맵 · 3: 그 B-tree
4: 볼륨 슈퍼블록(APSB) · 5: 볼륨 오브젝트 맵 · 6: 그 B-tree · 7~: 파일시스템 트리 노드 · 그 뒤: 파일 데이터
"""
import struct

from apfsrecover.apfsmeta import fletcher64

BS = 4096


def obj(oid, xid, typ, sub, body, size=BS):
    b = bytearray(size)
    struct.pack_into("<QQII", b, 8, oid, xid, typ, sub)
    b[32:32 + len(body)] = body
    struct.pack_into("<Q", b, 0, fletcher64(bytes(b)))
    return bytes(b)


def seal(b):
    b = bytearray(b)
    struct.pack_into("<Q", b, 0, fletcher64(bytes(b)))
    return bytes(b)


def node(oid, xid, entries, leaf=True, root=True, fixed=False, level=0, subtype=0xE):
    """B-tree 노드 하나. entries: [(key bytes, value bytes)]."""
    b = bytearray(BS)
    flags = (1 if root else 0) | (2 if leaf else 0) | (4 if fixed else 0)
    toc_entry = 4 if fixed else 8
    tlen = toc_entry * len(entries)
    kstart = 56 + tlen
    vend = BS - (40 if root else 0)
    ko = vo = 0
    for i, (k, v) in enumerate(entries):
        b[kstart + ko:kstart + ko + len(k)] = k
        vo += len(v)
        b[vend - vo:vend - vo + len(v)] = v
        if fixed:
            struct.pack_into("<HH", b, 56 + 4 * i, ko, vo)
        else:
            struct.pack_into("<HHHH", b, 56 + 8 * i, ko, len(k), vo, len(v))
        ko += len(k)
    struct.pack_into("<HHI", b, 32, flags, level, len(entries))
    struct.pack_into("<HH", b, 40, 0, tlen)
    struct.pack_into("<QQII", b, 8, oid, xid, 0x40000003 if not root else 0x40000002, subtype)
    return seal(b)


def jkey(oid, typ):
    return struct.pack("<Q", (typ << 60) | oid)


def drec(parent, name, child, hashed=True):
    nb = name.encode("utf-8") + b"\0"
    key = jkey(parent, 9) + (struct.pack("<I", len(nb) & 0x3FF) if hashed else struct.pack("<H", len(nb))) + nb
    return key, struct.pack("<QQH", child, 0, 8)


def inode(oid, parent):
    return jkey(oid, 3), struct.pack("<QQ", parent, oid) + b"\0" * 76


def extent(oid, phys, length):
    return jkey(oid, 8) + struct.pack("<Q", 0), struct.pack("<QQQ", length, phys, 0)


def build(files, block_count, volname="Macintosh HD", two_level=False, encrypted=False, xid=10, hashed=True):
    """files: [(경로 'Photos/IMG_0001.JPG', 물리 블록, 길이)]. 반환: {블록번호: 블록 bytes}."""
    blocks = {}
    nx = bytearray(4096 - 32)
    struct.pack_into("<IIQ", nx, 0, 0x4253584E, BS, block_count)
    struct.pack_into("<II", nx, 104 - 32, 1, 0)                 # xp_desc_blocks
    struct.pack_into("<qq", nx, 112 - 32, 1, 0)                 # xp_desc_base
    struct.pack_into("<QQQ", nx, 152 - 32, 0, 2, 0)             # omap_oid = 2
    struct.pack_into("<I", nx, 180 - 32, 1)
    struct.pack_into("<Q", nx, 184 - 32, 1026)                  # 볼륨 가상 oid
    blocks[0] = obj(1, xid, 0x80000001, 0, bytes(nx))
    blocks[1] = obj(1, xid, 0x80000001, 0, bytes(nx))
    om = struct.pack("<IIII", 0, 0, 0x40000002, 0) + struct.pack("<Q", 3)
    blocks[2] = obj(2, xid, 0x4000000B, 0, om)
    blocks[3] = node(3, xid, [(struct.pack("<QQ", 1026, xid), struct.pack("<IIQ", 0, BS, 4))], fixed=True, subtype=0xB)
    apsb = bytearray(4096 - 32)
    struct.pack_into("<I", apsb, 0, 0x42535041)
    struct.pack_into("<Q", apsb, 56 - 32, 0x1 if hashed else 0)
    struct.pack_into("<QQ", apsb, 128 - 32, 5, 1028)            # omap_oid(물리 5), root_tree_oid(가상 1028)
    struct.pack_into("<Q", apsb, 264 - 32, 0 if encrypted else 1)
    nm = volname.encode()
    apsb[704 - 32:704 - 32 + len(nm)] = nm
    blocks[4] = obj(1026, xid, 0x0000000D, 0, bytes(apsb))
    blocks[5] = obj(5, xid, 0x4000000B, 0, om[:16] + struct.pack("<Q", 6))
    # 파일시스템 기록
    recs, dirs, nid = [inode(2, 1)], {"": 2}, 16
    for path, phys, length in files:
        parts = path.split("/")
        parent = 2
        for i, p in enumerate(parts[:-1]):
            key = "/".join(parts[:i + 1])
            if key not in dirs:
                dirs[key] = nid
                recs += [inode(nid, parent), drec(parent, p, nid, hashed)]
                nid += 1
            parent = dirs[key]
        recs += [inode(nid, parent), drec(parent, parts[-1], nid, hashed), extent(nid, phys, length)]
        nid += 1
    if not two_level:
        blocks[7] = node(1028, xid, recs)
        vom = [(struct.pack("<QQ", 1028, xid), struct.pack("<IIQ", 0, BS, 7))]
    else:
        half = len(recs) // 2
        blocks[8] = node(1029, xid, recs[:half], root=False)
        blocks[9] = node(1030, xid, recs[half:], root=False)
        blocks[7] = node(1028, xid, [(recs[0][0], struct.pack("<Q", 1029)), (recs[half][0], struct.pack("<Q", 1030))], leaf=False, level=1)
        vom = [(struct.pack("<QQ", o, xid), struct.pack("<IIQ", 0, BS, p)) for o, p in ((1028, 7), (1029, 8), (1030, 9))]
    blocks[6] = node(6, xid, vom, fixed=True, subtype=0xB)
    return blocks


def write_image(path, blocks, total_blocks, data=()):
    """blocks 와 data([(블록번호, bytes)]) 를 담은 이미지 파일."""
    with open(path, "wb") as f:
        f.truncate(total_blocks * BS)
        for n, b in blocks.items():
            f.seek(n * BS)
            f.write(b)
        for n, d in data:
            f.seek(n * BS)
            f.write(d)
