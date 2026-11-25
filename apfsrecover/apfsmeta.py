"""APFS 파일 목록(이름·폴더) 읽기 — 원래 이름과 폴더 되살리기용(읽기 전용).

순서: 컨테이너 슈퍼블록(NXSB) → 체크섬이 맞는 가장 최근 체크포인트 → 컨테이너 오브젝트 맵 → 볼륨 슈퍼블록(APSB)
      → 볼륨 오브젝트 맵 → 파일시스템 트리(B-tree)를 끝까지 따라가며 폴더 항목(이름)과 파일 조각(시작 블록)을 모은다.
결과: 파일의 첫 조각이 있는 물리 블록 번호 → "볼륨/폴더/파일이름".
꺼낸 파일은 디스크 위치(offset)를 알고 있으므로, 같은 위치에서 시작하는 파일이면 원래 이름을 붙일 수 있다.

한계(추측하지 않는다):
- 체크섬이 맞지 않는 블록은 건너뛴다. 목록이 손상된 부분의 파일은 이름을 찾지 못한다.
- 암호화된 볼륨(FileVault)은 목록이 암호화돼 있어 읽지 못한다.
- 압축된 파일(데이터 조각이 없는 파일)은 위치로 맞출 수 없다.
참고: Apple File System Reference.
"""
import struct

from .nxsb import NX_MAGIC, fletcher64_ok, parse_nxsb

APSB_MAGIC = 0x42535041  # 'APSB'
T_NXSB, T_BTREE, T_NODE, T_OMAP, T_FS = 0x1, 0x2, 0x3, 0xB, 0xD
J_INODE, J_EXTENT, J_DREC = 3, 8, 9
ROOT_DIR = 2
BTNODE_ROOT, BTNODE_LEAF, BTNODE_FIXED = 1, 2, 4
INCOMPAT_CASE_INSENSITIVE, INCOMPAT_NORM_INSENSITIVE = 0x1, 0x8
FS_UNENCRYPTED = 0x1
MAX_NODES = 2_000_000
M = 0xFFFFFFFF


def fletcher64(block):
    """APFS 체크섬 값(처음 8바이트를 뺀 나머지로 계산). 시험용 이미지 만들기에도 쓴다."""
    words = struct.unpack("<%dI" % ((len(block) - 8) // 4), block[8:])
    s1 = s2 = 0
    for w in words:
        s1 = (s1 + w) % M
        s2 = (s2 + s1) % M
    c1 = M - ((s1 + s2) % M)
    c2 = M - ((s1 + c1) % M)
    return (c2 << 32) | c1


class Damaged(Exception):
    pass


class Container:
    def __init__(self, read_at, base=0, block_size=4096):
        self.read_at, self.base, self.bs = read_at, base, block_size
        self.bad = 0

    def block(self, paddr, need_type=None):
        b = self.read_at(self.base + paddr * self.bs, self.bs)
        if len(b) < self.bs or not fletcher64_ok(b):
            self.bad += 1
            return None
        if need_type is not None and (struct.unpack_from("<I", b, 24)[0] & 0xFFFF) != need_type:
            self.bad += 1
            return None
        return b

    # ---- B-tree ----
    def walk(self, root_paddr, resolve=None, leaf=None):
        """B-tree 를 끝까지 따라가며 잎 노드의 (key, value) 를 leaf 로 넘긴다. resolve(oid)->paddr 는 가상 주소용."""
        stack, seen, n = [root_paddr], set(), 0
        while stack:
            p = stack.pop()
            if p is None or p in seen:
                continue
            seen.add(p)
            n += 1
            if n > MAX_NODES:
                raise Damaged("트리가 너무 큽니다(손상 의심)")
            b = self.block(p)
            if b is None:
                continue
            flags, level, nkeys = struct.unpack_from("<HHI", b, 32)
            toff, tlen = struct.unpack_from("<HH", b, 40)
            fixed = bool(flags & BTNODE_FIXED)
            toc = 56 + toff
            kstart = toc + tlen
            vend = self.bs - (40 if flags & BTNODE_ROOT else 0)
            for i in range(min(nkeys, (tlen // (4 if fixed else 8)) or nkeys)):
                if fixed:
                    ko, vo = struct.unpack_from("<HH", b, toc + 4 * i)
                    key = b[kstart + ko:kstart + ko + 16]
                    val = b[vend - vo:vend - vo + (16 if flags & BTNODE_LEAF else 8)]
                else:
                    ko, kl, vo, vl = struct.unpack_from("<HHHH", b, toc + 8 * i)
                    key = b[kstart + ko:kstart + ko + kl]
                    val = b[vend - vo:vend - vo + vl]
                if flags & BTNODE_LEAF:
                    leaf(key, val)
                elif len(val) >= 8:
                    child = struct.unpack_from("<Q", val)[0]
                    stack.append(resolve(child) if resolve else child)

    def omap(self, omap_paddr, max_xid=None):
        """오브젝트 맵 전체: {oid: paddr} (같은 oid 는 max_xid 이하 중 가장 최근)."""
        b = self.block(omap_paddr, T_OMAP)
        if b is None:
            raise Damaged("오브젝트 맵 블록이 손상됐습니다")
        tree = struct.unpack_from("<Q", b, 48)[0]
        best = {}

        def leaf(k, v):
            oid, xid = struct.unpack_from("<QQ", k)
            flags, size, paddr = struct.unpack_from("<IIQ", v)
            if max_xid is not None and xid > max_xid:
                return
            if flags & 0x1:  # OMAP_VAL_DELETED
                return
            if oid not in best or xid > best[oid][0]:
                best[oid] = (xid, paddr)
        self.walk(tree, None, leaf)
        return {o: p for o, (x, p) in best.items()}


def checkpoints(read_at, base=0, scan_bytes=256 << 20):
    """체크섬이 맞는 NXSB 들을 xid 가 큰 순서로. 블록 0 → 체크포인트 영역 → (없으면) 앞쪽 scan_bytes 를 훑는다."""
    found = {}
    b0 = read_at(base, 4096)
    sb0 = None
    if len(b0) == 4096 and struct.unpack_from("<I", b0, 32)[0] == NX_MAGIC:
        sb0 = parse_nxsb(b0)
        bs = sb0["block_size"] if sb0["block_size"] in (4096, 8192, 16384, 32768, 65536) else 4096
        full = read_at(base, bs)
        if fletcher64_ok(full):
            found[sb0["xid"]] = (0, sb0)
        if 0 < sb0["xp_desc_base"] and 0 < sb0["xp_desc_blocks"] < 100000:
            for i in range(sb0["xp_desc_blocks"]):
                b = read_at(base + (sb0["xp_desc_base"] + i) * bs, bs)
                if len(b) == bs and struct.unpack_from("<I", b, 32)[0] == NX_MAGIC and fletcher64_ok(b):
                    sb = parse_nxsb(b)
                    found[sb["xid"]] = (sb0["xp_desc_base"] + i, sb)
    if not found:
        step = 4096
        for off in range(0, scan_bytes, 1 << 20):
            chunk = read_at(base + off, 1 << 20)
            for i in range(0, len(chunk) - step + 1, step):
                b = chunk[i:i + step]
                if struct.unpack_from("<I", b, 32)[0] == NX_MAGIC and fletcher64_ok(b):
                    sb = parse_nxsb(b)
                    found[sb["xid"]] = ((off + i) // step, sb)
            if len(chunk) < 1 << 20:
                break
    return [found[x][1] for x in sorted(found, reverse=True)]


def _name(key, hashed):
    if hashed and len(key) >= 12:
        nl = struct.unpack_from("<I", key, 8)[0] & 0x3FF
        raw = key[12:12 + nl]
    elif len(key) >= 10:
        nl = struct.unpack_from("<H", key, 8)[0]
        raw = key[10:10 + nl]
    else:
        return None
    return raw.split(b"\0")[0].decode("utf-8", "replace")


def read_volume(c, nx_omap, fs_oid, xid):
    """볼륨 하나: {"name", "encrypted", "files": {첫 물리 블록: 경로}, "nfiles", "dirs"} 또는 None."""
    p = nx_omap.get(fs_oid)
    if p is None:
        return None
    b = c.block(p, T_FS)
    if b is None or struct.unpack_from("<I", b, 32)[0] != APSB_MAGIC:
        return None
    incompat = struct.unpack_from("<Q", b, 56)[0]
    omap_oid, root_oid = struct.unpack_from("<QQ", b, 128)
    fs_flags = struct.unpack_from("<Q", b, 264)[0]
    name = b[704:704 + 256].split(b"\0")[0].decode("utf-8", "replace") or "볼륨"
    if not fs_flags & FS_UNENCRYPTED:
        return {"name": name, "encrypted": True, "files": {}, "nfiles": 0, "dirs": 0}
    vomap = c.omap(omap_oid)
    hashed = bool(incompat & (INCOMPAT_CASE_INSENSITIVE | INCOMPAT_NORM_INSENSITIVE))
    children, private, first = {}, {}, {}

    def leaf(k, v):
        if len(k) < 8:
            return
        hdr = struct.unpack_from("<Q", k)[0]
        oid, typ = hdr & ((1 << 60) - 1), hdr >> 60
        if typ == J_DREC and len(v) >= 8:
            nm = _name(k, hashed)
            if nm is not None:
                children[struct.unpack_from("<Q", v)[0]] = (oid, nm)
        elif typ == J_INODE and len(v) >= 16:
            private[struct.unpack_from("<Q", v, 8)[0]] = oid
        elif typ == J_EXTENT and len(k) >= 16 and len(v) >= 16:
            logical = struct.unpack_from("<Q", k, 8)[0]
            if logical == 0:
                pb = struct.unpack_from("<Q", v, 8)[0]
                if pb:
                    first[oid] = pb
    c.walk(vomap.get(root_oid, root_oid), lambda o: vomap.get(o, o), leaf)

    def path(fid):
        parts, n = [], 0
        while fid in children and n < 256:
            parent, nm = children[fid]
            parts.append(nm)
            if parent == ROOT_DIR:
                break
            fid, n = parent, n + 1
        else:
            if fid != ROOT_DIR and parts:
                parts.append("(위치 모름)")
        return "/".join(reversed(parts))
    files = {}
    for priv, pb in first.items():
        fid = private.get(priv, priv)
        if fid in children:
            files[pb] = path(fid)
    return {"name": name, "encrypted": False, "files": files, "nfiles": len(files), "dirs": len(set(p for p, _ in children.values()))}


def read_names(read_at, base=0):
    """반환: {"ok", "block_size", "volumes": [...], "by_block": {블록: "볼륨/경로"}, "xid", "bad_blocks", "error"}"""
    try:
        cps = checkpoints(read_at, base)
    except OSError as e:
        return {"ok": False, "error": "읽지 못했습니다: %s" % e, "volumes": [], "by_block": {}}
    if not cps:
        return {"ok": False, "error": "APFS 시작 표시(체크섬이 맞는 컨테이너 슈퍼블록)를 찾지 못했어요.", "volumes": [], "by_block": {}}
    last_err = None
    for sb in cps[:8]:  # 가장 최근 체크포인트부터, 안 되면 그 전 것으로
        c = Container(read_at, base, sb["block_size"])
        try:
            nx_omap = c.omap(sb["omap_oid"], sb["xid"])
            vols = [v for v in (read_volume(c, nx_omap, o, sb["xid"]) for o in sb["fs_oids"]) if v]
        except (Damaged, OSError, struct.error) as e:
            last_err = str(e)
            continue
        if not vols:
            last_err = "볼륨 정보를 찾지 못했어요"
            continue
        by_block = {}
        for v in vols:
            for pb, p in v["files"].items():
                by_block.setdefault(pb, v["name"] + "/" + p)
        return {"ok": True, "block_size": sb["block_size"], "xid": sb["xid"], "bad_blocks": c.bad, "error": None,
                "volumes": [{"name": v["name"], "encrypted": v["encrypted"], "files": v["nfiles"]} for v in vols], "by_block": by_block}
    return {"ok": False, "error": "파일 목록이 손상돼 읽지 못했어요" + (" (%s)" % last_err if last_err else ""), "volumes": [], "by_block": {}}
