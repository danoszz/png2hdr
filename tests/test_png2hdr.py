"""Test suite for png2hdr.

Covers the colour maths, the ICC builder, both container writers, the retag
guard, and the inspector. Everything runs in-memory or under tmp_path :: no
network and no fixtures on disk.
"""
from __future__ import annotations

import io
import warnings

import numpy as np
import pytest
from PIL import Image, ImageCms

import png2hdr
from png2hdr import cli, icc
from png2hdr import (
    M_709_TO_2020,
    inject_shadow_chroma,
    inspect,
    iter_chunks,
    pq_code_8bit,
    pq_oetf,
    resolve_anti_greyscale,
    retag_png,
    srgb_to_linear,
    stats,
    to_nits,
    write_jpeg,
    write_png,
)


# ------------------------------------------------------------------ helpers

def _png_bytes(arr: np.ndarray) -> bytes:
    """Encode a uint8/uint16 RGB array to PNG bytes with Pillow."""
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def _pq_eotf(signal: np.ndarray) -> np.ndarray:
    """Inverse of pq_oetf :: PQ signal in [0, 1] -> luminance in cd/m^2."""
    m1, m2 = 0.1593017578125, 78.84375
    c1, c2, c3 = 0.8359375, 18.8515625, 18.6875
    vm = np.clip(signal, 0.0, 1.0) ** (1.0 / m2)
    y = (np.maximum(vm - c1, 0.0) / (c2 - c3 * vm)) ** (1.0 / m1)
    return y * 10000.0


def _sample_rgb() -> np.ndarray:
    """A small colourful gradient with saturated, non-neutral pixels."""
    yy, xx = np.mgrid[0:32, 0:32]
    return np.stack([xx * 8, yy * 8, (31 - xx) * 8], -1).astype(np.uint8)


# ---------------------------------------------------------------- PQ OETF

@pytest.mark.parametrize("nits, anchor", [(100.0, 0.5081), (1000.0, 0.7518)])
def test_pq_oetf_anchors(nits, anchor):
    got = float(pq_oetf(np.array([nits]))[0])
    assert got == pytest.approx(anchor, abs=5e-4)


def test_pq_oetf_endpoints():
    # The pure ST 2084 OETF of 0 is a tiny epsilon (c1**m2), not exactly 0, but
    # it must encode to code 0 at both bit depths. 10000 cd/m^2 maps to 1.0.
    zero = float(pq_oetf(np.array([0.0]))[0])
    assert round(zero * 65535) == 0
    assert round(zero * 255) == 0
    assert float(pq_oetf(np.array([10000.0]))[0]) == pytest.approx(1.0, abs=1e-9)


def test_pq_oetf_roundtrips():
    nits = np.array([0.0, 1.0, 100.0, 203.0, 1000.0, 4000.0, 10000.0])
    assert np.allclose(_pq_eotf(pq_oetf(nits)), nits, rtol=1e-4, atol=1e-3)


def test_pq_oetf_monotonic():
    v = pq_oetf(np.linspace(0.0, 10000.0, 500))
    assert np.all(np.diff(v) >= 0.0)


# --------------------------------------------------------------------- ICC

def test_icc_parses_under_imagecms():
    profile = icc.build()
    parsed = ImageCms.ImageCmsProfile(io.BytesIO(profile))
    assert ImageCms.getProfileName(parsed).strip() == "Rec2020 Gamut with PQ Transfer"


def test_icc_cicp_tag_is_bt2100_pq():
    assert png2hdr._icc_cicp(icc.build()) == [9, 16, 0, 1]


def test_icc_size_is_reasonable():
    # README documents ~2.6 KB. Guard against silent bloat or truncation.
    assert 2000 < len(icc.build()) < 4000


# ---------------------------------------------------------------- JPEG ICC

def test_jpeg_embeds_and_survives_load():
    nits, _ = to_nits(Image.fromarray(_sample_rgb()), "flat", 1000.0, 203.0, 0.75)
    profile = icc.build()
    blob = write_jpeg(nits, profile)

    reloaded = Image.open(io.BytesIO(blob))
    embedded = reloaded.info.get("icc_profile")
    assert embedded, "ICC profile missing after load"
    assert png2hdr._icc_cicp(embedded) == [9, 16, 0, 1]
    # It must still parse as a colour profile, not just carry bytes.
    ImageCms.ImageCmsProfile(io.BytesIO(embedded))


def test_jpeg_icc_survives_pillow_resave():
    nits, _ = to_nits(Image.fromarray(_sample_rgb()), "flat", 1000.0, 203.0, 0.75)
    blob = write_jpeg(nits, icc.build())

    first = Image.open(io.BytesIO(blob))
    buf = io.BytesIO()
    first.save(buf, format="JPEG", icc_profile=first.info["icc_profile"])
    second = Image.open(io.BytesIO(buf.getvalue()))
    assert png2hdr._icc_cicp(second.info["icc_profile"]) == [9, 16, 0, 1]


# ---------------------------------------------------------------- PNG chunks

def test_png_chunk_order():
    nits, _ = to_nits(Image.fromarray(_sample_rgb()), "flat", 1000.0, 203.0, 0.75)
    tags = [tag for tag, _ in iter_chunks(write_png(nits))]

    assert tags[0] == b"IHDR"
    assert tags[-1] == b"IEND"
    # cICP, mDCV, cLLI sit between IHDR and the first IDAT, in that order.
    order = [tags.index(t) for t in (b"IHDR", b"cICP", b"mDCV", b"cLLI")]
    assert order == sorted(order)
    assert tags.index(b"cLLI") < tags.index(b"IDAT")


def test_png_signals_bt2100_pq():
    nits, _ = to_nits(Image.fromarray(_sample_rgb()), "flat", 1000.0, 203.0, 0.75)
    chunks = dict(
        (tag, raw[8:-4]) for tag, raw in iter_chunks(write_png(nits))
    )
    assert list(chunks[b"cICP"][:4]) == [9, 16, 0, 1]


# ------------------------------------------------------------------- retag

def test_retag_refuses_midtone():
    mid = np.full((16, 16, 3), 128, np.uint8)
    with pytest.raises(ValueError, match="neither 0 nor full scale"):
        retag_png(_png_bytes(mid))


def test_retag_accepts_midtone_with_force():
    mid = np.full((16, 16, 3), 128, np.uint8)
    out = retag_png(_png_bytes(mid), force=True)
    tags = [tag for tag, _ in iter_chunks(out)]
    assert b"cICP" in tags


def test_retag_accepts_pure_and_adds_cicp():
    pure = np.zeros((16, 16, 3), np.uint8)
    pure[4:12, 4:12] = 255
    out = retag_png(_png_bytes(pure))
    chunks = {tag: raw[8:-4] for tag, raw in iter_chunks(out)}
    assert list(chunks[b"cICP"][:4]) == [9, 16, 0, 1]
    # cICP is inserted immediately after IHDR.
    tags = [tag for tag, _ in iter_chunks(out)]
    assert tags[1] == b"cICP"


def test_retag_strips_competing_chunks():
    pure = np.zeros((16, 16, 3), np.uint8)
    pure[4:12, 4:12] = 255
    # Give the source an sRGB chunk that must not coexist with cICP.
    buf = io.BytesIO()
    Image.fromarray(pure).save(buf, format="PNG")
    src = buf.getvalue()
    tagged = retag_png(src)
    tags = [tag for tag, _ in iter_chunks(tagged)]
    for competing in (b"iCCP", b"sRGB", b"gAMA", b"cHRM"):
        assert competing not in tags


# --------------------------------------------------------- flat hue-preserving

def test_flat_mode_is_hue_preserving():
    rgb = _sample_rgb()
    img = Image.fromarray(rgb)
    nits, gain = to_nits(img, "flat", 1000.0, 203.0, 0.75)

    # Flat gain is a single scalar across the whole frame.
    assert gain.min() == pytest.approx(gain.max())

    lin = np.clip(srgb_to_linear(np.asarray(rgb, np.float64) / 255.0)
                  @ M_709_TO_2020.T, 0.0, 1.0)
    mask = lin.sum(-1) > 1e-6
    lin_chroma = lin[mask] / lin[mask].sum(-1, keepdims=True)
    nits_chroma = nits[mask] / nits[mask].sum(-1, keepdims=True)
    # Channel ratios in linear light are identical before and after gain.
    assert np.allclose(lin_chroma, nits_chroma, atol=1e-9)


# ------------------------------------------------- no spurious FPE warnings

def test_luma_paths_emit_no_runtime_warnings():
    # numpy 2.x matmul reduction sets false divide-by-zero / overflow FPE flags
    # on large frames. stats() and knee luma must stay silent. A mostly-black
    # 512x512 frame reproduces the trigger seen on real logo art.
    frame = np.zeros((512, 512, 3), np.uint8)
    frame[128:384, 128:384] = 255
    img = Image.fromarray(frame)
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        for mode in ("flat", "knee"):
            nits, _ = to_nits(img, mode, 1000.0, 203.0, 0.75)
            stats(nits)


# ----------------------------------------------------------------- inspector

def _write(path, blob):
    path.write_bytes(blob)
    return str(path)


def test_inspect_classifies_sdr(tmp_path, capsys):
    buf = io.BytesIO()
    Image.fromarray(_sample_rgb()).save(buf, format="JPEG", quality=90)
    rc = inspect(_write(tmp_path / "sdr.jpg", buf.getvalue()))
    out = capsys.readouterr().out
    assert rc == 0
    assert "no HDR signalling" in out


def test_inspect_classifies_pq_jpeg(tmp_path, capsys):
    nits, _ = to_nits(Image.fromarray(_sample_rgb()), "flat", 1000.0, 203.0, 0.75)
    rc = inspect(_write(tmp_path / "pq.jpg", write_jpeg(nits, icc.build())))
    out = capsys.readouterr().out
    assert rc == 0
    assert "HDR signalled" in out
    assert "PQ (ST 2084)" in out


def test_inspect_classifies_pq_png(tmp_path, capsys):
    nits, _ = to_nits(Image.fromarray(_sample_rgb()), "flat", 1000.0, 203.0, 0.75)
    rc = inspect(_write(tmp_path / "pq.png", write_png(nits)))
    out = capsys.readouterr().out
    assert rc == 0
    assert "HDR signalled" in out
    assert "[9, 16, 0, 1]" in out


# ----------------------------------------------- anti-greyscale (the trap)

def _neutral_logo(size=64):
    """A neutral mark on black :: every channel equal, at greyscale-trap risk."""
    a = np.zeros((size, size, 3), np.uint8)
    a[size // 4:3 * size // 4, size // 4:3 * size // 4] = 255
    return a


def test_anti_greyscale_auto_detects_neutral_vs_chromatic():
    neutral = to_nits(Image.fromarray(_neutral_logo()), "flat", 1600.0, 203.0, 0.75)[0]
    chroma = to_nits(Image.fromarray(_sample_rgb()), "flat", 1000.0, 203.0, 0.75)[0]
    assert resolve_anti_greyscale("auto", neutral) == 12   # near-neutral -> inject
    assert resolve_anti_greyscale("auto", chroma) == 0     # chromatic -> leave it
    assert resolve_anti_greyscale("off", neutral) == 0     # explicit off
    assert resolve_anti_greyscale(20, chroma) == 20        # forced level


def test_anti_greyscale_spread_survives_q96_roundtrip():
    nits = to_nits(Image.fromarray(_neutral_logo()), "flat", 1600.0, 203.0, 0.75)[0]
    blob = write_jpeg(nits, icc.build(), quality=96, anti_greyscale=12)
    img = Image.open(io.BytesIO(blob))
    assert img.mode == "RGB"                               # genuinely 3-component
    arr = np.asarray(img.convert("RGB")).astype(np.int16)
    spread = arr.max(-1) - arr.min(-1)
    assert spread.max() >= 8                               # equality broken, survived JPEG


def test_anti_greyscale_leaves_the_mark_bit_identical():
    nits = to_nits(Image.fromarray(_neutral_logo()), "flat", 1600.0, 203.0, 0.75)[0]
    plain = pq_code_8bit(nits)
    injected = inject_shadow_chroma(plain, 12)
    lum = plain.mean(-1)
    bright = lum > np.percentile(lum, 60)                  # the mark, above the shadow line
    assert np.array_equal(plain[bright], injected[bright])


def test_injected_chroma_is_imperceptible():
    black = np.zeros((32, 32, 3), np.uint8)               # every pixel is shadow
    injected = inject_shadow_chroma(black, 12)
    lum_nits = png2hdr._luma(_pq_eotf(injected.astype(np.float64) / 255.0))
    assert lum_nits.max() < 0.1                            # under 0.1 cd/m^2, invisible


def test_inspect_flags_greyscale_reencode(tmp_path, capsys):
    # What LinkedIn serves back :: 1-component greyscale, RGB profile still attached.
    grey = np.zeros((48, 48), np.uint8)
    grey[16:32, 16:32] = 190
    buf = io.BytesIO()
    Image.fromarray(grey).save(buf, format="JPEG", quality=95, icc_profile=icc.build())
    rc = inspect(_write(tmp_path / "grey.jpg", buf.getvalue()))
    out = capsys.readouterr().out
    assert rc == 0
    assert "(greyscale)" in out
    assert "WARNING" in out
    assert "greyscale re-encode" in out


def test_inspect_reports_component_count(tmp_path, capsys):
    nits = to_nits(Image.fromarray(_sample_rgb()), "flat", 1000.0, 203.0, 0.75)[0]
    inspect(_write(tmp_path / "rgb.jpg", write_jpeg(nits, icc.build())))
    out = capsys.readouterr().out
    assert any("components" in ln and ln.strip().endswith("3") for ln in out.splitlines())


# ------------------------------------------------------------ CLI smoke test

def test_cli_flat_jpeg_end_to_end(tmp_path):
    src = tmp_path / "logo.png"
    Image.fromarray(_sample_rgb()).save(src, format="PNG")
    dst = tmp_path / "out.jpg"
    rc = cli.main([str(src), "-o", str(dst), "--peak", "1000"])
    assert rc == 0
    assert dst.exists()
    embedded = Image.open(dst).info.get("icc_profile")
    assert png2hdr._icc_cicp(embedded) == [9, 16, 0, 1]


def test_cli_retag_refusal_exit_code(tmp_path, capsys):
    src = tmp_path / "mid.png"
    Image.fromarray(np.full((16, 16, 3), 128, np.uint8)).save(src, format="PNG")
    rc = cli.main([str(src), "-o", str(tmp_path / "x.png"), "--mode", "retag"])
    assert rc == 1
    assert "neither 0 nor full scale" in capsys.readouterr().err
