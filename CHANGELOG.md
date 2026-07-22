# Changelog

All notable changes to png2hdr. Format follows [Keep a Changelog](https://keepachangelog.com).

## [0.2.3] - 2026-07-22

### Added
- `--anti-greyscale` (default `auto`). Verified in served bytes :: LinkedIn re-encodes any
  JPEG whose channels are equal everywhere as a 1-component greyscale image. The ICC profile
  stays attached and byte-identical but still declares `space = RGB` while the data is now
  Gray, so the renderer discards it on the mismatch and the PQ samples read as sRGB. The
  result is a flat grey logo with perfectly intact metadata, and it is invisible until you
  inspect what the CDN serves back. The fix injects a trace of chroma into the shadows only
  (blue floor 12, red floor 4 in PQ code, ~0.05 cd/m^2), which breaks channel equality,
  survives JPEG at q96 4:4:4, and never touches the mark. `auto` fires only on near-neutral
  input; `off` disables; an integer sets the level.
- `inspect` now reports JPEG component count and emits a loud warning on the greyscale
  signature (1 component carrying an RGB ICC profile, or a greyscale-colour-type PNG with a
  cICP chunk). That combination is otherwise completely silent.

### Changed
- Softened the MaxFALL note. The old text implied MaxFALL above ~500 was the cause of
  failure; it never explained the neutral-asset failures, which were the greyscale bug. There
  are two independent modes :: the greyscale re-encode (fixed above) and a frame-average
  overrun that is n=2 and unconfirmed since the fix. The note now says so, and the ~500 line
  stays a nudge to check the served file rather than a limit.
- README gains a "greyscale trap" section and corrects the peak and status sections. The
  generated ICC profile is now stated as never having been through a live upload, since every
  measured success used a LUT-based third-party profile.

## [0.2.2] - 2026-07-22

### Fixed
- Lowered `requires-python` from 3.10 to 3.9. The floor was never justified :: every
  module already uses `from __future__ import annotations` and there is no 3.10-only
  syntax. macOS ships 3.9, so the old floor blocked the default interpreter.
- Use `Image.Resampling.LANCZOS` with a fallback. `Image.LANCZOS` is removed in
  newer Pillow.
- Dropped the deprecated `mode` argument from the JPEG writer's `Image.fromarray`
  call. Pillow 13 removes that parameter and the dependency has no upper bound, so
  the call would raise on a current Pillow. A 3-channel uint8 array infers `RGB`,
  so the output bytes are unchanged.
- Compute BT.2020 luma as an elementwise weighted sum instead of `rgb @ LUMA_2020`.
  On numpy 2.x the matmul vector-reduction kernel sets spurious divide-by-zero and
  overflow FPE flags on larger frames, so a normal logo conversion printed alarming
  `RuntimeWarning: divide by zero encountered in matmul` lines to stderr. The result
  is bit-identical :: only the false warnings are gone. It surfaced on a 512x512
  asset here but not on the small sandbox fixtures, because the flags only trip
  above a size threshold.

### Added
- Test suite under `tests/` (pytest) :: PQ OETF anchors and round-trip, ICC parse
  under `ImageCms` with a `[9,16,0,1]` cicp tag, JPEG ICC save/load survival, PNG
  chunk ordering, the retag guard, flat-mode hue preservation, and inspector
  classification of SDR, PQ JPEG, and PQ PNG files.
- CI workflow running the suite across the supported Python versions (3.9 to 3.13).
- README explainer :: the origin brief, a "First principles" walk from SDR white through
  colour spaces, linear light, absolute nits, and the ST 2084 and sRGB maths in LaTeX,
  ending on the container-survival hack. Written to read from beginner to expert.

## [0.2.1] - 2026-07-22

### Changed
- MaxFALL warning threshold lowered from 800 to 500, based on observed uploads.
- Install docs lead with a virtualenv path instead of assuming pipx.

## [0.2.0] - 2026-07-22

### Added
- JPEG output carrying an ICC v4 profile with a `cicp` tag, now the default. ICC
  profiles survive upload pipelines that discard PNG `cICP` chunks.
- Generated ICC v4.4 profile (~2.6 KB) built from BT.2020 primaries, a sampled PQ
  tone curve, and `cicp` code points 9/16/0/1. No third-party profile redistributed.
- `--inspect` for local files and URLs. Decodes PNG chunks, JPEG segments, ICC `cicp`
  tags, and probes for MPF and Ultra HDR gain maps.
- `--neutral-blue` for saturated sources whose blue channel is genuinely zero.
- `--resize`, `--icc`, `--quality`, `--bg`.
- MaxFALL warning on `--dry-run`.

### Changed
- Restructured into an installable package with a `png2hdr` console script.
- `retag` now refuses images with intermediate sample values unless `--force`.

## [0.1.0]

### Added
- PQ conversion with `flat`, `knee`, and `retag` modes.
- 16-bit PNG output with `cICP`, `mDCV`, and `cLLI` chunks.
