#!/usr/bin/env python3
"""png2hdr :: convert images to BT.2100 PQ HDR in a container that survives.

Two output paths:

  jpg  8-bit PQ pixels + an ICC v4 profile carrying a `cicp` tag.
       Ugly on paper, but ICC profiles are preserved by essentially every
       image pipeline, so this is what reaches viewers on real platforms.

  png  16-bit PQ pixels + the PNG Third Edition `cICP`/`mDCV`/`cLLI` chunks.
       Higher fidelity and the correct modern answer. Also silently stripped
       by most upload pipelines, which do not recognise the chunks.
"""
from __future__ import annotations

import argparse
import struct
import sys
import zlib
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

from . import icc as _icc

__version__ = "0.2.2"

try:                                    # Pillow >= 9.1
    LANCZOS = Image.Resampling.LANCZOS
except AttributeError:                  # Pillow < 9.1
    LANCZOS = Image.LANCZOS

# ---------------------------------------------------------------- colour maths

M_709_TO_2020 = np.array([
    [0.627404, 0.329283, 0.043313],
    [0.069097, 0.919540, 0.011362],
    [0.016391, 0.088013, 0.895595],
])
LUMA_2020 = np.array([0.2627, 0.6780, 0.0593])

CICP_BT2100_PQ = bytes([9, 16, 0, 1])   # BT.2020 / PQ / RGB / full range
DIFFUSE_WHITE_NITS = 203.0              # ITU-R BT.2408 reference white
COMPETING_CHUNKS = (b"iCCP", b"sRGB", b"gAMA", b"cHRM", b"cICP", b"mDCV", b"cLLI")

# macOS ships a suitable profile; prefer it over the generated one when present.
SYSTEM_ICC_HINTS = [
    "/System/Library/ColorSync/Profiles/Rec2020_PQ.icc",
    "/System/Library/ColorSync/Profiles/ITU-R BT.2100 PQ.icc",
    "/usr/share/color/icc/colord/Rec2020.icc",
]


def srgb_to_linear(c: np.ndarray) -> np.ndarray:
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def pq_oetf(nits: np.ndarray) -> np.ndarray:
    """Absolute luminance (cd/m^2) -> PQ signal in [0, 1]. SMPTE ST 2084."""
    y = np.clip(nits / 10000.0, 0.0, 1.0)
    m1, m2 = 0.1593017578125, 78.84375
    c1, c2, c3 = 0.8359375, 18.8515625, 18.6875
    ym = y ** m1
    return ((c1 + c2 * ym) / (1.0 + c3 * ym)) ** m2


def _luma(rgb: np.ndarray) -> np.ndarray:
    """BT.2020 luminance of an (..., 3) array.

    Elementwise weighted sum rather than `rgb @ LUMA_2020`. The matmul vector
    reduction path raises spurious divide-by-zero and overflow FPE warnings on
    numpy 2.x for larger frames, though the numeric result is bit-identical.
    """
    return (rgb * LUMA_2020).sum(-1)


# --------------------------------------------------------------- tone mapping

def to_nits(img: Image.Image, mode: str, peak: float, white: float,
            knee: float, bg=(0, 0, 0), neutral_blue: bool = False):
    """sRGB image -> absolute BT.2020 luminance, plus the gain field applied."""
    if img.mode in ("RGBA", "LA", "P", "PA"):
        img = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", img.size, (*bg, 255)), img)
    rgb = np.asarray(img.convert("RGB"), dtype=np.float64) / 255.0

    lin = np.clip(srgb_to_linear(rgb) @ M_709_TO_2020.T, 0.0, 1.0)
    if neutral_blue:
        # Sources with B=0 gain a small blue term from the primaries change.
        # PQ is steep near black, so that term desaturates badly if the profile
        # is ever stripped. Only safe when the source really has no blue.
        lin[..., 2] = 0.0

    if mode == "flat":
        gain = np.full(lin.shape[:2], peak / max(1e-9, lin.max() * white))
    elif mode == "knee":
        luma = _luma(lin)
        u = np.clip((luma - knee) / max(1e-6, 1.0 - knee), 0.0, 1.0)
        u = u * u * (3.0 - 2.0 * u)          # smoothstep :: no hard edge
        gain = 1.0 + u * (peak / white - 1.0)
    else:
        raise ValueError(f"unknown mode {mode!r}")

    return lin * white * gain[..., None], gain


def stats(nits: np.ndarray) -> dict:
    luma = _luma(nits)
    p50, p90, p99 = np.percentile(luma, [50, 90, 99])
    return {"p50": p50, "p90": p90, "p99": p99,
            "maxcll": float(nits.max()), "maxfall": float(luma.mean())}


# ------------------------------------------------------------------- ICC source

def resolve_icc(explicit: str | None = None) -> tuple[bytes, str]:
    if explicit:
        return Path(explicit).read_bytes(), explicit
    for path in SYSTEM_ICC_HINTS:
        p = Path(path)
        if p.is_file():
            return p.read_bytes(), path
    return _icc.build(), "generated"


# ------------------------------------------------------------------ PNG writer

def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def iter_chunks(blob: bytes):
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    pos = 8
    while pos < len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        yield blob[pos + 4:pos + 8], blob[pos:pos + 12 + length]
        pos += 12 + length


def _mdcv(max_nits: float, min_nits: float = 0.0001) -> bytes:
    # PNG Third Edition orders primaries R, G, B (not the G, B, R of HEVC SEI).
    return _chunk(b"mDCV", struct.pack(
        ">8HII",
        35400, 14600, 8500, 39850, 6550, 2300, 15635, 16450,
        int(round(max_nits * 10000)), max(1, int(round(min_nits * 10000)))))


def write_png(nits: np.ndarray, extra: bytes = b"") -> bytes:
    code = np.rint(pq_oetf(nits) * 65535.0).astype(np.uint16).astype(">u2")
    h, w, _ = code.shape
    idat = zlib.compress(
        b"".join(b"\x00" + code[y].tobytes() for y in range(h)), 9)
    s = stats(nits)
    out = bytearray(b"\x89PNG\r\n\x1a\n")
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 2, 0, 0, 0))
    out += _chunk(b"cICP", CICP_BT2100_PQ)
    out += _mdcv(s["maxcll"])
    out += _chunk(b"cLLI", struct.pack(
        ">II", int(round(s["maxcll"] * 10000)), int(round(s["maxfall"] * 10000))))
    out += extra
    out += _chunk(b"IDAT", idat)
    out += _chunk(b"IEND", b"")
    return bytes(out)


def retag_png(blob: bytes, force: bool = False) -> bytes:
    """Label an existing PNG without touching pixels.

    Only valid when every sample sits at 0 or full scale. Anything else has its
    channel ratios mangled, because PQ and sRGB disagree everywhere between.
    """
    arr = np.asarray(Image.open(BytesIO(blob)).convert("RGB"))
    full = 255 if arr.dtype == np.uint8 else 65535
    impure = float(np.mean((arr != 0) & (arr != full)))
    if impure > 0.001 and not force:
        raise ValueError(
            f"{impure:.1%} of samples are neither 0 nor full scale. Retagging "
            "would shift hue badly. Use --mode flat or --mode knee, or --force.")
    out = bytearray(blob[:8])
    for tag, raw in iter_chunks(blob):
        if tag in COMPETING_CHUNKS:
            continue
        out += raw
        if tag == b"IHDR":
            out += _chunk(b"cICP", CICP_BT2100_PQ)
    return bytes(out)


# ----------------------------------------------------------------- JPEG writer

def write_jpeg(nits: np.ndarray, profile: bytes, quality: int = 96) -> bytes:
    code = np.rint(pq_oetf(nits) * 255.0).clip(0, 255).astype(np.uint8)
    buf = BytesIO()
    Image.fromarray(code).save(
        buf, format="JPEG", quality=quality, subsampling=0,
        progressive=True, icc_profile=profile)
    return buf.getvalue()


# -------------------------------------------------------------------- inspector

def _icc_cicp(profile: bytes):
    if len(profile) < 132:
        return None
    n = struct.unpack(">I", profile[128:132])[0]
    for k in range(n):
        off = 132 + k * 12
        if off + 12 > len(profile):
            break
        sig, o, s = struct.unpack(">4sII", profile[off:off + 12])
        if sig == b"cicp" and o + 12 <= len(profile):
            return list(profile[o + 8:o + 12])
    return None


PRIMARIES = {1: "BT.709", 9: "BT.2020", 12: "Display P3"}
TRANSFER = {1: "BT.709", 8: "linear", 13: "sRGB", 16: "PQ (ST 2084)", 18: "HLG"}


def _describe(cicp) -> str:
    p, t = cicp[0], cicp[1]
    return (f"{cicp} :: {PRIMARIES.get(p, '?')} / {TRANSFER.get(t, '?')} / "
            f"matrix {cicp[2]} / {'full' if cicp[3] else 'limited'} range")


def inspect(target: str) -> int:
    if target.startswith(("http://", "https://")):
        from urllib.request import Request, urlopen
        req = Request(target, headers={"User-Agent": "png2hdr"})
        with urlopen(req, timeout=30) as r:
            blob = r.read()
    else:
        blob = Path(target).read_bytes()

    print(f"{target}\n  {len(blob):,} bytes")
    hdr, cicp, profile = False, None, None

    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        print("  container      PNG")
        for tag, raw in iter_chunks(blob):
            body = raw[8:-4]
            note = ""
            if tag == b"IHDR":
                w, h, bd, ct = struct.unpack(">IIBB", body[:10])
                note = f"{w}x{h} {bd}-bit colour-type {ct}"
            elif tag == b"cICP":
                cicp = list(body[:4]); note = _describe(cicp)
            elif tag == b"cLLI":
                cll, fall = struct.unpack(">II", body[:8])
                note = f"MaxCLL {cll/10000:.0f} MaxFALL {fall/10000:.0f} cd/m^2"
            elif tag == b"iCCP":
                profile = zlib.decompress(body.split(b"\0", 1)[1][1:])
                note = f"embedded profile, {len(profile)} bytes"
            print(f"  {tag.decode('latin1'):14s} {len(body):7,}  {note}")
            if tag == b"IDAT":
                break

    elif blob[:2] == b"\xff\xd8":
        print("  container      JPEG")
        i, segs, prog = 2, [], False
        while i < len(blob) - 1:
            if blob[i] != 0xFF:
                i += 1; continue
            m = blob[i + 1]
            if m == 0xD8:
                print("  SOI            second image present (gain map?)"); i += 2; continue
            if m == 0xD9 or m == 0x01 or 0xD0 <= m <= 0xD7:
                i += 2; continue
            ln = struct.unpack(">H", blob[i + 2:i + 4])[0]
            payload = blob[i + 4:i + 4 + ln - 2]
            if m == 0xC2:
                prog = True
            if 0xE0 <= m <= 0xEF:
                name = payload[:20].split(b"\0")[0].decode("latin1", "replace")
                segs.append((f"APP{m - 0xE0}", ln, name))
                if name == "ICC_PROFILE":
                    profile = payload[14:]
            if m == 0xDA:
                break
            i += 2 + ln
        for n, ln, name in segs:
            print(f"  {n:14s} {ln:7,}  {name}")
        print(f"  encoding       {'progressive' if prog else 'baseline'}")
        for probe, label in ((b"MPF\x00", "MPF gain map"),
                             (b"hdrgm", "Ultra HDR gain map"),
                             (b"urn:iso:std:iso:ts:21496", "ISO 21496-1 gain map")):
            if probe in blob:
                print(f"  {label:14s} present"); hdr = True
    else:
        print("  container      unrecognised"); return 1

    if profile:
        c = _icc_cicp(profile)
        print(f"  ICC            {len(profile):,} bytes, "
              f"cicp tag {'-> ' + _describe(c) if c else 'absent'}")
        if c and c[1] in (16, 18):
            cicp = c

    print()
    if cicp and cicp[1] in (16, 18):
        print(f"  VERDICT  HDR signalled :: {TRANSFER.get(cicp[1])}. "
              "Should drive display headroom.")
    elif hdr:
        print("  VERDICT  gain map present :: HDR on supporting viewers.")
    else:
        print("  VERDICT  no HDR signalling found :: renders as SDR.")
    return 0
