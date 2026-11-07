# Changelog

## 2026.11.07

Quality and usability release on top of the initial 2026.11.04
version. All renders produced by earlier versions remain fully
compatible and re-renderable.

### Rendering & camera

- Content-aware framing: the camera now fits the real isosurface
  extent measured from the cube (with automatic bohr → angstrom
  conversion), so orbital lobes, MEP density surfaces and ELF
  domains are never cropped. Adjustable fit margin.
- Property-map color ranges (MEP etc.) are auto-filled from
  percentiles instead of raw min/max, which the near-nuclear
  electrostatic potential used to wash out.
- New neutral gray background (#2b2b2b); the High Gloss preset now
  uses it instead of pure black.

### User interface

- The render dialog is now scrollable and the Render button bar
  stays fixed and always visible.
- Fixed "Open Scene in VMD": VMD exits when its stdin reaches EOF,
  so the button now launches it inside a pseudo-terminal
  (platform-specific, verified). Every render project also ships an
  `open_in_vmd.sh` launcher that opens the scene from anywhere —
  double-click included.
- Cross-platform VMD detection (Linux, Windows, macOS) with a
  diagnostics dialog that names the missing dependency with per-OS
  guidance.

### Examples & documentation

- Six fully reproducible ORCA/Multiwfn examples (orbitals, MEP,
  spin density, ELF) with user-approved viewing angles and English
  step-by-step guides, plus a quickstart.
- Visual README (comparison banners, clickable full-resolution
  gallery) and a References section citing MoleditPy, VMD, ORCA and
  Multiwfn.

### Repository hygiene

- No personal calculation data: the private test cube was replaced
  by a synthetic in-memory fixture; example inputs are generic.
- External binaries (ORCA, Multiwfn) are located via environment
  variables, PATH or a home-relative layout — no hardcoded
  usernames.

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
