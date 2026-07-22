"""Command line interface for png2hdr."""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

from PIL import Image

from . import (DIFFUSE_WHITE_NITS, LANCZOS, __version__, inspect,
               resolve_anti_greyscale, resolve_icc, retag_png, stats, to_nits,
               write_jpeg, write_png)



def parse_hex(s: str):
    s = s.lstrip("#")
    if len(s) != 6:
        raise argparse.ArgumentTypeError("expected #rrggbb")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def anti_greyscale_arg(s: str):
    if s in ("auto", "off"):
        return s
    try:
        v = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError("expected 'auto', 'off', or an integer 0-255")
    if not 0 <= v <= 255:
        raise argparse.ArgumentTypeError("level must be 0-255")
    return v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="png2hdr",
        description="Convert images to BT.2100 PQ HDR (JPEG+ICC or PNG+cICP).",
        epilog="Use --format jpg for anything you intend to upload. Most "
               "pipelines strip PNG cICP chunks but preserve ICC profiles.")
    ap.add_argument("src", help="source image, or a file/URL with --inspect")
    ap.add_argument("-o", "--out")
    ap.add_argument("--inspect", action="store_true",
                    help="report HDR signalling in an existing file or URL")
    ap.add_argument("--format", choices=("jpg", "png", "auto"), default="auto",
                    help="auto follows the -o extension, else jpg")
    ap.add_argument("--mode", choices=("flat", "knee", "retag"), default="flat",
                    help="flat: uniform gain (logos, flat fields). "
                         "knee: lift highlights only (photos). "
                         "retag: label PNG without converting (pure 0/max art)")
    ap.add_argument("--peak", type=float, default=1000.0,
                    help="target peak in cd/m^2 (default 1000)")
    ap.add_argument("--white", type=float, default=DIFFUSE_WHITE_NITS,
                    help="diffuse white in cd/m^2 (default 203, BT.2408)")
    ap.add_argument("--knee", type=float, default=0.75,
                    help="linear luma where knee mode starts lifting")
    ap.add_argument("--bg", type=parse_hex, default=(0, 0, 0),
                    help="composite colour for alpha input (default #000000)")
    ap.add_argument("--neutral-blue", action="store_true",
                    help="zero the blue channel after the primaries change; "
                         "only for sources whose blue is genuinely 0")
    ap.add_argument("--anti-greyscale", type=anti_greyscale_arg, default="auto",
                    metavar="MODE",
                    help="break channel equality in the shadows so a pipeline "
                         "cannot re-encode near-neutral art to 1-component "
                         "greyscale and orphan the ICC profile. auto (default), "
                         "off, or a PQ code level. jpg only")
    ap.add_argument("--icc", help="ICC profile to embed (default: system, "
                                  "else a generated Rec2020-PQ profile)")
    ap.add_argument("--quality", type=int, default=96, help="JPEG quality")
    ap.add_argument("--resize", type=int, metavar="N", help="square resize to NxN")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the luminance report and write nothing")
    ap.add_argument("--version", action="version", version=f"png2hdr {__version__}")
    a = ap.parse_args(argv)

    try:
        if a.inspect:
            return inspect(a.src)

        fmt = a.format
        if fmt == "auto":
            fmt = "png" if (a.out or "").lower().endswith(".png") else "jpg"
        if a.mode == "retag":
            fmt = "png"
        dst = a.out or f"{Path(a.src).stem}_hdr.{fmt}"

        if a.mode == "retag":
            blob = Path(a.src).read_bytes()
            if a.dry_run:
                print("retag :: pixels unchanged, cICP 9/16/0/1 added")
                return 0
            Path(dst).write_bytes(retag_png(blob, force=a.force))
            print(f"{dst} :: retagged BT.2100 PQ, pixels unchanged")
            return 0

        img = Image.open(a.src)
        if a.resize:
            img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
            img = img.resize((a.resize, a.resize), LANCZOS)
        nits, gain = to_nits(img, a.mode, a.peak, a.white, a.knee,
                             a.bg, a.neutral_blue)
        s = stats(nits)
        report = (f"gain {gain.min():.2f}-{gain.max():.2f}x :: luma p50 "
                  f"{s['p50']:.0f} p90 {s['p90']:.0f} p99 {s['p99']:.0f} :: "
                  f"MaxCLL {s['maxcll']:.0f} MaxFALL {s['maxfall']:.0f} cd/m^2")
        if s["maxfall"] > 500:
            report += "\n  note: MaxFALL above ~500 may trip frame-average tone " \
                      "mapping on some displays. That mode is n=2 and unconfirmed " \
                      "since the greyscale fix :: a nudge to check the served file."

        ag_level = resolve_anti_greyscale(a.anti_greyscale, nits) if fmt == "jpg" else 0
        if ag_level:
            report += (f"\n  anti-greyscale :: +{ag_level} shadow chroma so a "
                       "greyscale re-encode cannot orphan the profile")

        if a.dry_run:
            print(report)
            return 0

        if fmt == "jpg":
            profile, src = resolve_icc(a.icc)
            Path(dst).write_bytes(write_jpeg(nits, profile, a.quality, ag_level))
            extra = f"ICC {len(profile)}B ({src})"
        else:
            Path(dst).write_bytes(write_png(nits))
            extra = "cICP + mDCV + cLLI"

        h, w = nits.shape[:2]
        print(f"{dst} :: {w}x{h} {fmt} :: {extra}\n  {report}")
        return 0

    except (ValueError, OSError, struct.error) as exc:
        print(f"png2hdr: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
