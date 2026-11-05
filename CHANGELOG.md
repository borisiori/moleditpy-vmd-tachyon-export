# Changelog

## 2026.11.04 — Initial release

First public version of **VMD-Tachyon Export**, a MoleditPy plugin that
turns the current molecule (and Gaussian cube data) into
publication-quality images using a local VMD installation as an
external Tachyon rendering backend.

### Rendering

* Three entry points (Export 3D button, File → Export, Plugins menu).
* High Quality default preset plus Publication White/Dark, High Gloss,
  Scientific Matte and Minimal Clean — all fully editable and
  persistent between sessions.
* External Tachyon renderer for exact resolutions
  (`-trans_raster3d -fullshade`, AA up to 32 samples); built-in
  TachyonInternal fallback.
* Editable, reproducible render projects: `scene.tcl` (relative
  paths), VMD state snapshot, re-render scripts (`render.tcl`,
  `render_edited.tcl`), `render_settings.json`, provenance manifest
  and per-project README.
* Exact VTK→VMD camera matching (verified within 1 pixel), preset
  orientations and VMD's own reference framing.
* Optional transparent background (documented post-process) and
  lossless PNG/TIFF conversion with a built-in numpy TGA decoder.

### Chemistry

* 1:1 atom/element/coordinate export from the RDKit molecule; exact
  connectivity through a generated PSF topology — no distance-based
  bond guessing (safe for radicals and transition states).
* Isosurface maps: two-lobe orbitals and spin density (blue/red),
  property maps on a density surface (BWR, percentile-based auto
  range), ELF domains; `.cub`/`.cube`/`.gz` recognized by content and
  auto-associated when the molecule comes from a cube file.

### Integration

* Cross-platform VMD detection (Linux, Windows, macOS) with env-var
  overrides and a diagnostics dialog that names the missing dependency
  with per-OS guidance.
* Non-blocking GUI (QProcess pipeline, full logging, safe
  cancellation of its own child processes only).

### Repository

* 34 unit tests + an end-to-end suite that runs the real pipeline
  against the local VMD installation.
* Comparison banners (presets, 22 materials, representations) and six
  reproducible ORCA/Multiwfn examples with step-by-step guides.

Verified against MoleditPy 5.1.2 and VMD 2.0.0 (LINUXAMD64) on
CachyOS/Arch Linux.
