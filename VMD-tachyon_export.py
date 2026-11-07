#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VMD-Tachyon Export — render MoleditPy molecules and volumetric data with a
local VMD installation through its Tachyon ray-tracing engine.

Integration points (same pattern as the bundled POV-Ray export plugin):
  * "Export 3D" toolbar button menu  -> "Export to VMD-Tachyon Scene..."
  * File > Export menu               -> "Export to VMD-Tachyon Scene..."
  * Plugins menu (run() entry point) -> "VMD-Tachyon Export"

Every render produces a self-contained, editable project folder:

  <output_root>/molecule_NNN/
      geometry/molecule.pdb|.psf|.xyz   exact RDKit coordinates + connectivity
      volumetric/*.cube                 copied cube data, when used
      scene/scene.tcl                   readable, editable scene (relative paths)
      scene/build_scene.tcl             initial build: scene + save_state + render
      scene/render.tcl                  re-render from scene.tcl (no re-export)
      scene/render_edited.tcl           re-render from a manually edited scene.vmd
      scene/render_settings.json        all parameters used
      tachyon/scene.dat                 Tachyon scene (external engine only)
      output/render.tga|.png(_trans)    final images
      logs/vmd_render.log               full process transcript
      manifest.json                     provenance: versions, engine, paths, params
      README.md                         how to open, edit and re-render

Verified against VMD 2.0.0 (LINUXAMD64) in text mode (``-dispdev none``).
"""

import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ------------------------------------------------------------------ metadata
PLUGIN_NAME = "VMD-Tachyon Export"
PLUGIN_VERSION = "2026.11.04"
PLUGIN_AUTHOR = "borisiori"
PLUGIN_LICENSE = "LGPL-3.0-or-later"
PLUGIN_DESCRIPTION = (
    "Render the current molecule (and Gaussian cube data) with a local VMD "
    "installation using the Tachyon engine, producing publication-quality "
    "images plus fully editable, reproducible VMD scene projects."
)
PLUGIN_DEPENDENCIES = ["PySide6-Essentials", "numpy"]
PLUGIN_SUPPORTED_MOLEDITPY_VERSION = ">=5.0.0, <6.0.0"
# No PLUGIN_SUPPORTED_OS: the plugin is not OS-restricted (the registry
# expects that constant only for restricted plugins). VMD itself is an
# external program available for Linux, Windows and macOS.
PLUGIN_TAGS = ["Rendering", "Export", "3D"]

PLUGIN_CONTEXT = None

logger = logging.getLogger(__name__)

# Settings keys persisted through context.get_setting/set_setting
SETTING_VMD_PATH = "vmd_path"
SETTING_OUTPUT_ROOT = "output_root"
SETTING_GUI_STATE = "gui_state"
SETTING_CUSTOM_PRESETS = "custom_presets"

# Persisted-GUI-state schema version. When the defaults change in a way
# that must reach existing installations (e.g. the high_quality
# quality preset replacing older ones), bump this: state persisted by an
# older plugin is then ignored instead of silently overriding the new
# quality settings with stale values.
GUI_STATE_VERSION = 5

# Camera calibration constants for VMD 2.0.0 measured with identity matrices
# (see README "Camera matching"): at scale_matrix = 1.0 the visible half-height
# in scene units is constant regardless of window size.
VMD_ORTHO_HALFHEIGHT = 1.8204   # orthographic, at the focal plane
VMD_PERSP_HALFHEIGHT = 1.4971   # perspective, at the focal (z=0) plane


# ===========================================================================
# 1. Plugin registration
# ===========================================================================

def initialize(context):
    """Register the export action shown in the Export 3D button, the
    File > Export menu (both driven by add_export_action) and keep the
    context for the Plugins-menu run() entry point."""
    global PLUGIN_CONTEXT
    PLUGIN_CONTEXT = context
    context.add_export_action(
        "Export to VMD-Tachyon Scene...", lambda: _show_dialog(context)
    )


def run(mw):
    """Plugins-menu entry point (legacy run() protocol, like povray_export)."""
    if PLUGIN_CONTEXT is None:
        return
    _show_dialog(PLUGIN_CONTEXT)


def _show_dialog(context):
    """Create or raise the single export dialog (orca_calculator pattern)."""
    from PySide6.QtWidgets import QDialog

    dlg = context.get_window("dialog")
    if dlg is not None:
        try:
            if dlg.isVisible():
                dlg.raise_()
                dlg.activateWindow()
                return
            if dlg.close() is False:
                dlg.show()
                dlg.raise_()
                return  # a render is still shutting down
            dlg.deleteLater()
        except RuntimeError:
            pass
        context.register_window("dialog", None)

    from PySide6.QtCore import Qt

    dialog_class = _lazy_dialog_class()
    mw = context.get_main_window()
    new_dlg = dialog_class(mw, context, version=PLUGIN_VERSION)
    new_dlg.setWindowFlag(Qt.WindowMinMaxButtonsHint, True)
    context.register_window("dialog", new_dlg)
    new_dlg.show()


# ===========================================================================
# 2. VMD discovery and validation
# ===========================================================================

class VmdInstallation:
    """A located VMD launcher plus what was verified about it."""

    def __init__(self, vmd_path="", tachyon_path="", version=""):
        self.vmd_path = vmd_path
        self.tachyon_path = tachyon_path
        self.version = version

    @property
    def has_external_tachyon(self):
        return bool(self.tachyon_path)

    def as_dict(self):
        return {
            "vmd_path": self.vmd_path,
            "tachyon_path": self.tachyon_path,
            "version": self.version,
        }


def _current_os():
    """'windows' | 'macos' | 'linux' (mapped from sys.platform)."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _vmd_search_roots():
    """OS-specific parent folders searched for VMD installations.

    Linux covers Arch/CachyOS-style manual unpacks; Windows the default
    installer locations; macOS /Applications plus brew/macports prefixes.
    """
    home = Path.home()
    if sys.platform == "win32":
        roots = []
        for var, default in (
            ("ProgramFiles", r"C:\Program Files"),
            ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ):
            roots.append(Path(os.environ.get(var) or default))
        local = os.environ.get("LocalAppData")
        if local:
            roots.append(Path(local) / "Programs")
        roots.append(home / "AppData" / "Local" / "Programs")
        roots.append(home)
        return roots
    if sys.platform == "darwin":
        return [
            Path("/Applications"),
            home / "Applications",
            Path("/usr/local"),
            Path("/opt/homebrew"),
            Path("/opt/local"),
            home,
        ]
    return [
        Path("/opt"),
        Path("/usr/local"),
        home,
        home / "Programas",
        home / "apps",
        home / ".local",
        home / "src",
    ]


def _vmd_install_folder_names():
    """Folder-name needles matched (case-insensitively) inside the roots."""
    if sys.platform == "win32":
        return ("vmd", "university of illinois")
    return ("vmd",)


def _vmd_launcher_suffixes():
    """Launcher paths tried inside each matched install folder."""
    if sys.platform == "win32":
        return ("vmd.exe", "bin/vmd.exe")
    if sys.platform == "darwin":
        # drag-and-drop .app bundles, tarball installs and bin/ layouts
        return (
            "vmd",
            "bin/vmd",
            "Contents/MacOS/vmd",
            "Contents/MacOS/VMD",
            "Contents/Resources/VMD/vmd",
        )
    return ("bin/vmd", "vmd")


def _vmd_candidate_roots():
    """Conventional places VMD shows up, per OS. Yields launcher paths."""
    for parent in _vmd_search_roots():
        try:
            names = [
                n for n in os.listdir(parent)
                if any(needle in n.lower()
                       for needle in _vmd_install_folder_names())
            ]
        except OSError:
            continue
        for name in sorted(names, key=str.lower):
            for base in (parent / name, parent / (name + ".app")):
                for suffix in _vmd_launcher_suffixes():
                    yield base / suffix


def _plain_vmd_paths():
    """Well-known absolute launcher locations per OS."""
    if sys.platform == "darwin":
        return ("/usr/local/bin/vmd", "/opt/homebrew/bin/vmd",
                "/opt/local/bin/vmd")
    if sys.platform == "win32":
        return ()
    return ("/usr/bin/vmd", "/usr/local/bin/vmd")


def find_vmd():
    """Return the first plausible vmd launcher path ('' when none found).

    Order: env overrides, saved setting is NOT read here (the caller passes
    it), PATH, then conventional roots. Multi-platform (Linux, Windows,
    macOS).
    """
    candidates = []
    launcher_name = "vmd.exe" if sys.platform == "win32" else "vmd"

    for var in ("VMD_BIN", "VMD_HOME", "VMDDIR"):
        value = os.environ.get(var, "").strip()
        if value:
            p = Path(value)
            if p.is_file():
                candidates.append(p)
            else:
                candidates.append(p / launcher_name)
                candidates.append(p / "bin" / launcher_name)

    which = shutil.which(launcher_name) or shutil.which("vmd")
    if which:
        candidates.append(Path(which))

    candidates.extend(Path(plain) for plain in _plain_vmd_paths())
    candidates.extend(_vmd_candidate_roots())

    seen = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if _is_executable_file(candidate):
            return str(candidate.resolve())
    return ""


def _is_executable_file(path):
    try:
        p = Path(path)
        if not p.is_file():
            return False
        if sys.platform == "win32":
            return True  # no execute bit on Windows
        return os.access(str(p), os.X_OK)
    except OSError:
        return False


def validate_vmd(vmd_path):
    """Launch VMD in text mode and ask for its version.

    Returns (ok, message, version). Uses a controlled subprocess with a
    timeout; never opens a graphical session. The probe writes its markers
    to a FILE instead of stdout: the Windows launcher does not attach a
    console, so stdout capture is unreliable there — a result file works
    identically on Linux, Windows and macOS.
    """
    if not vmd_path:
        return False, "not set", ""
    if not Path(vmd_path).is_file():
        return False, "file not found: " + vmd_path, ""
    if not _is_executable_file(vmd_path):
        return False, "not executable: " + vmd_path, ""
    import tempfile

    probe_fd, probe_path = tempfile.mkstemp(suffix=".tcl",
                                            prefix="vmdprobe_")
    out_fd, out_path = tempfile.mkstemp(suffix=".txt",
                                        prefix="vmdprobe_out_")
    os.close(out_fd)
    try:
        # forward slashes survive on every OS; braces avoid Tcl quoting
        safe_out = out_path.replace("\\", "/")
        with os.fdopen(probe_fd, "w") as handle:
            handle.write(
                "if {[catch {open {%s} w} _pf]} {\n"
                "  puts {VMDPROBE-OPENFAIL}\n"
                "} else {\n"
                "  puts $_pf \"VMDPROBE-[vmdinfo version]\"\n"
                "  if {[catch {render TachyonInternal {%s}} e]} {\n"
                "    puts $_pf \"VMDPROBE-INTERNAL-FAIL: $e\"\n"
                "  } else {\n"
                "    puts $_pf \"VMDPROBE-INTERNAL-OK\"\n"
                "  }\n"
                "  close $_pf\n"
                "}\n"
                "exit\n" % (safe_out, safe_out + ".tga")
            )
        subprocess.run(
            [vmd_path, "-dispdev", "none", "-e", probe_path],
            input="", capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "VMD did not answer within 120 s", ""
    except OSError as e:
        return False, "could not launch VMD: " + str(e), ""
    finally:
        for temp in (probe_path, out_path + ".tga"):
            try:
                os.unlink(temp)
            except OSError:
                pass

    try:
        with open(out_path, "r", errors="replace") as handle:
            out = handle.read()
    except OSError:
        out = ""
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    match = re.search(r"VMDPROBE-([\d.]+)", out)
    if not match:
        if "VMDPROBE-OPENFAIL" in out:
            return False, "VMD ran but could not write its probe file", ""
        return False, "VMD ran but the probe produced no result", ""
    version = match.group(1)
    internal = "VMDPROBE-INTERNAL-OK" in out
    if not internal:
        # TachyonInternal is built into every stock VMD; failure here means
        # a very unusual build. Keep it usable but say so.
        return True, "VMD " + version + " (TachyonInternal unavailable?)", \
            version
    return True, "VMD " + version + " ready", version


def find_external_tachyon(vmd_path):
    """Locate the standalone Tachyon binary shipped with a VMD install.

    Preference: TACHYON_BIN env, PATH, then the install folders derived
    from the vmd launcher (bin/vmd -> ../..; on Windows/macOS the binary
    sits next to the launcher or inside lib/vmd/). The launcher script is
    also scanned for its VMDDIR definition. The tachyon_* glob matches
    the per-OS names (tachyon_LINUXAMD64, tachyon_Win32.exe,
    tachyon_MACOSX…).
    """
    env = os.environ.get("TACHYON_BIN", "").strip()
    if env and _is_executable_file(env):
        return str(Path(env).resolve())

    which = shutil.which("tachyon")
    if which:
        return str(Path(which).resolve())

    if not vmd_path:
        return ""
    roots = []
    launcher = Path(vmd_path).resolve()
    # bin/vmd layout (official tars) -> install root two levels up
    if launcher.parent.name == "bin":
        roots.append(launcher.parent.parent)
    roots.append(launcher.parent)
    # The shell launcher usually contains its own VMDDIR assignment.
    try:
        text = launcher.read_text(errors="replace")
        match = re.search(
            r'set\s+VMDDIR\s*=?\s*"?([^"\n;]+)"?', text
        ) or re.search(r'VMDDIR=["\']?([^"\'\n;]+)', text)
        if match:
            roots.append(Path(match.group(1).strip()))
    except OSError:
        pass

    for root in roots:
        for pattern in ("tachyon_*", "tachyon"):
            try:
                hits = sorted(root.glob(f"lib/vmd/{pattern}"))
            except OSError:
                continue
            hits += sorted(root.glob(pattern))
            for hit in hits:
                if _is_executable_file(hit):
                    return str(hit.resolve())
    return ""


def detect_installation(saved_path=""):
    """Full detection pass: saved path, auto-search, validation, tachyon."""
    vmd_path = saved_path.strip() if saved_path else ""
    if vmd_path and not _is_executable_file(vmd_path):
        vmd_path = ""
    if not vmd_path:
        vmd_path = find_vmd()
    if not vmd_path:
        return VmdInstallation()
    ok, _msg, version = validate_vmd(vmd_path)
    if not ok:
        return VmdInstallation(vmd_path=vmd_path)
    return VmdInstallation(
        vmd_path=vmd_path,
        tachyon_path=find_external_tachyon(vmd_path),
        version=version,
    )


DEPENDENCY_HINTS = {
    "windows": {
        "vmd": (
            "Missing: VMD itself. Install VMD for Windows (default folder: "
            "C:\\Program Files\\University of Illinois\\VMD\\vmd.exe). "
            "If it is already installed, add that folder to PATH or pick "
            "vmd.exe with Browse… in VMD Setup."
        ),
        "tachyon": (
            "Missing: the standalone Tachyon renderer, needed for exact "
            "output resolutions. Windows VMD ships tachyon_Win32.exe next "
            "to vmd.exe; set TACHYON_BIN or reinstall VMD if it is absent."
        ),
        "launch": (
            "VMD was found but did not run the probe. On Windows try "
            "running VMD once from its own folder, check your antivirus is "
            "not blocking it, and that this is a full (not zipped "
            "partial) install."
        ),
    },
    "macos": {
        "vmd": (
            "Missing: VMD itself. Install the VMD application (drag it to "
            "/Applications) or a Homebrew/MacPorts build, then pick its "
            "launcher with Browse… if auto-detection does not find it."
        ),
        "tachyon": (
            "Missing: the standalone Tachyon renderer, needed for exact "
            "output resolutions. macOS VMD ships it inside the application "
            "bundle (…/Contents/Resources/VMD/); set TACHYON_BIN or "
            "reinstall VMD if it is absent."
        ),
        "launch": (
            "VMD was found but did not run the probe. macOS may block "
            "downloaded binaries: run VMD once manually, or remove the "
            "quarantine flag with "
            "'xattr -d com.apple.quarantine <vmd>'."
        ),
    },
    "linux": {
        "vmd": (
            "Missing: VMD itself. Install the Linux VMD from "
            "https://www.ks.uiuc.edu/Research/vmd/ and make sure its "
            "'vmd' launcher is on PATH, under /opt or your home, or set "
            "the path in VMD Setup."
        ),
        "tachyon": (
            "Missing: the standalone Tachyon renderer, needed for exact "
            "output resolutions. It ships with VMD at "
            "<install>/lib/vmd/tachyon_LINUXAMD64; set TACHYON_BIN or "
            "reinstall VMD if it is absent."
        ),
        "launch": (
            "VMD was found but did not run the probe. Check that the "
            "launcher has execute permission (chmod +x) and that its "
            "libraries are intact."
        ),
    },
}


def dependency_hints():
    """Per-OS guidance for each missing dependency of the render pipeline."""
    return DEPENDENCY_HINTS[_current_os()]


def diagnose_installation(installation):
    """Full dependency report for 'Test VMD Installation'.

    Returns (lines, missing): lines describe every check; missing lists
    (dependency, os_specific_hint) pairs for everything the pipeline
    needs and does not have.
    """
    lines = []
    missing = []
    hints = dependency_hints()

    vmd = installation.vmd_path
    lines.append("Operating system: " + _current_os().capitalize())
    if vmd:
        lines.append("VMD executable: " + vmd)
        if installation.version:
            lines.append("Launch / Tcl: OK — VMD " + installation.version)
        else:
            lines.append("Launch / Tcl: FAILED")
            missing.append(("vmd-launch", hints["launch"]))
    else:
        lines.append("VMD executable: not found")
        missing.append(("vmd", hints["vmd"]))

    if installation.version:
        lines.append("TachyonInternal (built-in renderer): available")
    else:
        lines.append("TachyonInternal (built-in renderer): unknown "
                     "(needs a working VMD)")

    if installation.tachyon_path:
        lines.append("External Tachyon (exact resolutions): "
                     + installation.tachyon_path)
    else:
        lines.append("External Tachyon (exact resolutions): not found")
        if vmd:
            missing.append(("tachyon", hints["tachyon"]))

    try:
        import numpy  # noqa: F401

        lines.append("Python numpy (image handling): available")
    except ImportError:
        lines.append("Python numpy (image handling): MISSING")
        missing.append((
            "numpy",
            "numpy ships with MoleditPy; your installation seems broken — "
            "reinstall MoleditPy.",
        ))
    try:
        from PIL import Image  # noqa: F401

        lines.append("Python Pillow: available (TIFF output enabled)")
    except ImportError:
        lines.append(
            "Python Pillow: not installed (optional — only TIFF output "
            "uses it; TGA/PNG need nothing extra)"
        )
    return lines, missing


# ===========================================================================
# 3. Molecular geometry export (XYZ / PDB / PSF with exact connectivity)
# ===========================================================================

def mol_atom_data(mol):
    """Extract (symbols, positions Å, bonds) from an RDKit Mol.

    Bonds are (begin_idx, end_idx) 0-based pairs in RDKit order; the order
    is preserved 1:1 into every exported file (serial = idx + 1).
    """
    from rdkit import Chem  # noqa: F401 - typed access only

    conf = mol.GetConformer()
    symbols, positions = [], []
    for atom in mol.GetAtoms():
        symbols.append(atom.GetSymbol())
        pos = conf.GetAtomPosition(atom.GetIdx())
        positions.append((pos.x, pos.y, pos.z))
    bonds = [
        (b.GetBeginAtomIdx(), b.GetEndAtomIdx()) for b in mol.GetBonds()
    ]
    return symbols, positions, bonds


def write_xyz(symbols, positions, comment="exported by MoleditPy"):
    lines = [str(len(symbols)), comment]
    for symbol, (x, y, z) in zip(symbols, positions):
        lines.append(f"{symbol:<6s}{x:>14.6f}{y:>14.6f}{z:>14.6f}")
    return "\n".join(lines) + "\n"


def write_pdb(symbols, positions):
    """Minimal PDB: coordinates + element column, one residue."""
    lines = []
    for i, (symbol, (x, y, z)) in enumerate(zip(symbols, positions), 1):
        name = f"{symbol}{i}"[:4]
        element = f"{symbol[:2]:>2s}"
        lines.append(
            f"ATOM  {i:5d} {name:<4s} LIG A   1    "
            f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00          {element:2s}"
        )
    lines.append("END")
    return "\n".join(lines) + "\n"


def write_psf(symbols, positions, bonds):
    """XPLOR PSF carrying the exact RDKit connectivity.

    This is what VMD reads for topology: no distance-based bond guessing
    happens anywhere in the pipeline, which keeps radicals, transition
    states and unusual geometries chemically intact.
    """
    from rdkit import Chem

    pt = Chem.GetPeriodicTable()
    out = ["PSF \n\n", "       1 !NTITLE\n",
           "       topology generated by MoleditPy VMD-Tachyon Export\n\n"]
    out.append(f"{len(symbols):8d} !NATOM\n")
    for i, symbol in enumerate(symbols, 1):
        try:
            mass = pt.GetAtomicWeight(pt.GetAtomicNumber(symbol))
        except (RuntimeError, ValueError, TypeError):
            mass = 0.0
        out.append(
            f"{i:8d} {'L':<4s} {1:<4d} {'LIG':<4s} "
            f"{(symbol + str(i))[:4]:<4s} {symbol[:4]:<4s} "
            f"{0.0:10.6f} {mass:13.4f} {0:8d}\n"
        )
    out.append("\n")
    out.append(f"{len(bonds):8d} !NBOND: bonds\n")
    chunk = []
    for j, (b, e) in enumerate(bonds, 1):
        chunk.append(f"{b + 1:8d}{e + 1:8d}")
        if j % 4 == 0:
            out.append("".join(chunk) + "\n")
            chunk = []
    if chunk:
        out.append("".join(chunk) + "\n")
    return "".join(out)


def _atoms_from_cube_file(path):
    """Read atom symbols/positions straight from a cube header (atomic
    numbers are in the file; symbols derived through RDKit)."""
    from rdkit import Chem

    header = parse_cube_header(path)
    if header is None:
        return None
    import gzip

    path = str(path)
    with open(path, "rb") as probe:
        magic = probe.read(2)
    opener = (gzip.open if magic == b"\x1f\x8b" else open)
    pt = Chem.GetPeriodicTable()
    symbols, positions = [], []
    with opener(path, "rt", errors="replace") as handle:
        for _ in range(2 + 4 + header["extra_line"]):
            handle.readline()
        for _ in range(header["natoms"]):
            parts = handle.readline().split()
            number = abs(int(float(parts[0])))
            symbols.append(pt.GetElementSymbol(number))
            positions.append(tuple(float(x) for x in parts[2:5]))
    return symbols, positions


def load_external_geometry(path, current_mol=None):
    """Load an external geometry (XYZ or Gaussian cube); keep current
    connectivity when atom counts match.

    Returns (symbols, positions, bonds, message). Bonds are transferred
    from the current molecule when atom counts match; otherwise the export
    is unbonded on purpose (VMD never guesses bonds for us).
    """
    from rdkit import Chem

    suffix = Path(path).suffix.lower()
    data = None
    if suffix in (".cub", ".cube", ".gz"):
        data = _atoms_from_cube_file(path)
        if data is None:
            return None, None, None, f"could not read cube file: {path}"
    if data is None:
        mol = Chem.MolFromXYZFile(str(path))
        if mol is None:
            return None, None, None, f"could not read XYZ file: {path}"
        symbols, positions, bonds = mol_atom_data(mol)
    else:
        symbols, positions = data
        bonds = []
    message = ""
    if current_mol is not None:
        if current_mol.GetNumAtoms() == len(symbols):
            bonds = [
                (b.GetBeginAtomIdx(), b.GetEndAtomIdx())
                for b in current_mol.GetBonds()
            ]
            message = (
                "connectivity transferred from the current molecule "
                "(atom counts match)"
            )
        else:
            message = (
                "atom count differs from the current molecule: exported "
                "without bonds (no distance guessing)"
            )
    return symbols, positions, bonds, message


# ===========================================================================
# 4. Camera math — PyVista/VTK camera -> VMD display matrices
# ===========================================================================

def _normalize(v):
    length = math.sqrt(sum(c * c for c in v))
    if length < 1e-12:
        raise ValueError("cannot normalize zero-length vector")
    return tuple(c / length for c in v)


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def lookat_basis(eye, focal, up):
    """(right, up, back) orthonormal basis for a camera at eye looking at
    focal with reference up. 'back' points toward the viewer, matching
    VMD's eye space (+z out of the screen)."""
    forward = _normalize(tuple(f - e for f, e in zip(focal, eye)))
    right = _normalize(_cross(forward, up))
    true_up = _cross(right, forward)
    back = tuple(-c for c in forward)
    return right, true_up, back


def vtk_camera_to_vmd(camera, window_size):
    """Convert a VTK/PyVista camera to VMD (matrices, scale, projection).

    Returns dict with center, rotate rows (3x4), scale, projection, and
    matching half-height, or raises ValueError when data is degenerate.
    """
    pos = camera.GetPosition()
    focal = camera.GetFocalPoint()
    up = camera.GetViewUp()
    ortho = bool(camera.GetParallelProjection())
    right, up_b, back = lookat_basis(pos, focal, up)

    if ortho:
        half_height = float(camera.GetParallelScale())
        scale = VMD_ORTHO_HALFHEIGHT / half_height
        projection = "Orthographic"
    else:
        distance = math.sqrt(sum((p - f) ** 2 for p, f in zip(pos, focal)))
        angle = math.radians(float(camera.GetViewAngle()))
        half_height = distance * math.tan(angle / 2.0)
        scale = VMD_PERSP_HALFHEIGHT / half_height
        projection = "Perspective"

    return {
        "center": tuple(-c for c in focal),
        "rotate": [right, up_b, back],
        "scale": scale,
        "projection": projection,
        "half_height": half_height,
        "window_size": window_size,
    }


def preset_camera(orientation, positions, projection, auto_center=True,
                  auto_fit=True, custom_rot=(0.0, 0.0, 0.0),
                  fit_margin=1.15, fit_radius=None):
    """Compute a VMD camera for the named orientations.

    Bases (right, up, back=toward viewer) in world axes:
      front  +X +Y +Z   back  -X +Y -Z   top    +X +Z -Y
      bottom +X -Z +Y   left  +Z +Y -X   right  -Z +Y +X
    """
    bases = {
        "front": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
        "back": ((-1, 0, 0), (0, 1, 0), (0, 0, -1)),
        "top": ((1, 0, 0), (0, 0, 1), (0, -1, 0)),
        "bottom": ((1, 0, 0), (0, 0, -1), (0, 1, 0)),
        "left": ((0, 0, 1), (0, 1, 0), (-1, 0, 0)),
        "right": ((0, 0, -1), (0, 1, 0), (1, 0, 0)),
    }
    if orientation in bases:
        rotate = [list(b) for b in bases[orientation]]
    elif orientation == "isometric":
        back = _normalize((1.0, 1.0, 1.0))
        right = _normalize(_cross((0, 1, 0), back))
        up = _cross(back, right)
        rotate = [list(right), list(up), list(back)]
    else:  # custom Euler rotations applied to the front basis
        rx, ry, rz = (math.radians(a) for a in custom_rot)

        def rot_x(v):
            c, s = math.cos(rx), math.sin(rx)
            return (v[0], c * v[1] - s * v[2], s * v[1] + c * v[2])

        def rot_y(v):
            c, s = math.cos(ry), math.sin(ry)
            return (c * v[0] + s * v[2], v[1], -s * v[0] + c * v[2])

        def rot_z(v):
            c, s = math.cos(rz), math.sin(rz)
            return (c * v[0] - s * v[1], s * v[0] + c * v[1], v[2])

        compose = lambda v: rot_z(rot_y(rot_x(v)))  # noqa: E731
        rotate = [list(compose(b)) for b in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]

    if not positions:
        return {"center": (0.0, 0.0, 0.0), "rotate": rotate, "scale": 1.0,
                "projection": projection}

    if orientation == "auto_fit" and auto_fit:
        # VMD's own framing (identical to the reference sessions: Zoom 1)
        return {"resetview": True, "rotate": rotate, "scale": 1.0,
                "projection": projection}

    center = (0.0, 0.0, 0.0)
    if auto_center:
        n = len(positions)
        center = tuple(
            sum(p[i] for p in positions) / n for i in range(3)
        )

    scale = 1.0
    if auto_fit:
        r_max = max(
            math.sqrt(sum((p[i] - center[i]) ** 2 for i in range(3)))
            for p in positions
        )
        if r_max < 1e-6:
            r_max = 1.0
        r_max = fit_radius if fit_radius else r_max
        half_height = r_max * fit_margin
        const = (
            VMD_ORTHO_HALFHEIGHT
            if projection == "Orthographic"
            else VMD_PERSP_HALFHEIGHT
        )
        scale = const / half_height

    return {
        "center": tuple(-c for c in center),
        "rotate": rotate,
        "scale": scale,
        "projection": projection,
    }


# ===========================================================================
# 5. Cube parsing (header + data range for isovalue suggestion)
# ===========================================================================

def parse_cube_header(path):
    """Read the header of a Gaussian cube: comments, atom count, grid.

    Accepts plain and gzipped cubes (.cub, .cube, .gz). Returns dict or
    None. Does not scan the full data block.
    """
    import gzip

    path = str(path)
    try:
        with open(path, "rb") as probe:
            magic = probe.read(2)
        if magic == b"\x1f\x8b":
            opener = gzip.open(path, "rt", errors="replace")
        else:
            opener = open(path, "r", errors="replace")
        with opener as handle:
            lines = [handle.readline() for _ in range(2 + 4)]
            natoms_raw = int(lines[2].split()[0]) if len(lines) > 2 else 0
            for _ in range(abs(natoms_raw)):
                lines.append(handle.readline())
    except (OSError, EOFError):
        return None
    if len(lines) < 6:
        return None
    try:
        parts = lines[2].split()
        natoms = int(parts[0])
        origin = tuple(float(x) for x in parts[1:4])
        dims, vectors = [], []
        for line in lines[3:6]:
            p = line.split()
            dims.append(abs(int(p[0])))
            vectors.append(tuple(float(x) for x in p[1:4]))
        npts = dims[0] * dims[1] * dims[2]
        orbital = natoms < 0
        extra = 0
        if orbital:
            parts = lines[6].split() if len(lines) > 6 else []
            extra = 1
        atom_coords = []
        for line in lines[6:6 + abs(natoms)]:
            parts = line.split()
            atom_coords.append(
                tuple(float(x) for x in parts[2:5])
            )
        return {
            "comments": [lines[0].strip(), lines[1].strip()],
            "natoms": abs(natoms),
            "orbital_cube": orbital,
            "origin": origin,
            "dims": dims,
            "vectors": vectors,
            "atom_coords": atom_coords,
            "points": npts,
            "extra_line": extra,
            "grid_points": npts,
        }
    except (ValueError, IndexError):
        return None


def cube_data_range(path, header=None):
    """min/max of the cube data block (numpy scan; ~0.1 s for 500k points)."""
    import gzip

    import numpy as np

    path = str(path)
    header = header or parse_cube_header(path)
    if header is None:
        return None
    try:
        with open(path, "rb") as probe:
            magic = probe.read(2)
        if magic == b"\x1f\x8b":
            handle = gzip.open(path, "rt", errors="replace")
        else:
            handle = open(path, "r", errors="replace")
        with handle:
            for _ in range(2 + 4 + header["extra_line"] + header["natoms"]):
                handle.readline()
            tokens = handle.read().split()
        values = np.array(tokens, dtype=float)
    except (OSError, ValueError, EOFError):
        return None
    if values.size == 0:
        return None
    return float(values.min()), float(values.max())


def cube_data_percentiles(path, header=None, low=2.0, high=98.0):
    """Robust data range (percentiles) for color scaling.

    Full min/max is wrong for property maps like the electrostatic
    potential: values next to the nuclei diverge (ESP ~ +150 a.u. on a
    water grid) and would wash the whole surface to one end of the
    color scale. The 2-98th percentiles bracket the chemically
    meaningful range.
    """
    import gzip

    import numpy as np

    path = str(path)
    header = header or parse_cube_header(path)
    if header is None:
        return None
    try:
        with open(path, "rb") as probe:
            magic = probe.read(2)
        opener = (gzip.open if magic == b"\x1f\x8b" else open)
        with opener(path, "rt", errors="replace") as handle:
            for _ in range(2 + 4 + header["extra_line"]
                           + header["natoms"]):
                handle.readline()
            tokens = handle.read().split()
        values = np.array(tokens, dtype=float)
    except (OSError, ValueError, EOFError):
        return None
    if values.size == 0:
        return None
    return (float(np.percentile(values, low)),
            float(np.percentile(values, high)))


def cube_isosurface_radius(path, isovalue, header=None,
                           reference_positions=None):
    """Radius (from the data centroid) reached by the |value| >= isovalue
    region — used to FRAME renders that contain an isosurface.

    Fitting only the atomic skeleton would leave big surfaces (a 0.001
    a.u. density envelope, diffuse orbital lobes) cropped at the edges;
    this measures what will actually be visible.

    Cube files use bohr by convention (ORCA/Multiwfn included); the
    returned radius is converted to angstroms using the header's own
    atom coordinates against reference_positions when available.
    """
    import gzip

    import numpy as np

    path = str(path)
    header = header or parse_cube_header(path)
    if header is None:
        return None
    try:
        with open(path, "rb") as probe:
            magic = probe.read(2)
        opener = (gzip.open if magic == b"\x1f\x8b" else open)
        with opener(path, "rt", errors="replace") as handle:
            for _ in range(2 + 4 + header["extra_line"]
                           + header["natoms"]):
                handle.readline()
            tokens = handle.read().split()
        values = np.abs(np.array(tokens, dtype=float)).reshape(
            header["dims"][0], header["dims"][1], header["dims"][2]
        )
    except (OSError, ValueError, EOFError):
        return None
    mask = values >= abs(isovalue)
    if not mask.any():
        return None
    # grid coordinates in the cube's (generally orthonormal) frame
    nx, ny, nz = header["dims"]
    axes = [np.arange(n) for n in (nx, ny, nz)]
    gx, gy, gz = np.meshgrid(*axes, indexing="ij")
    pts = np.stack([gx[mask], gy[mask], gz[mask]], axis=1).astype(float)
    # scale each axis by its voxel vector length (A or bohr, cube units)
    lens = [
        float(np.linalg.norm(header["vectors"][i])) for i in range(3)
    ]
    pts *= np.array(lens)
    center = pts.mean(axis=0)
    radius = float(np.sqrt(((pts - center) ** 2).sum(axis=1)).max())

    # unit conversion: cube units (bohr by convention) -> angstroms,
    # derived from the header atoms themselves when references exist
    if reference_positions is not None and len(reference_positions) >= 2:
        ref = np.asarray(reference_positions, dtype=float)
        cube_atoms = np.array(header["atom_coords"], dtype=float)
        if len(cube_atoms) == len(ref):
            span_cube = cube_atoms.max(axis=0) - cube_atoms.min(axis=0)
            span_ref = ref.max(axis=0) - ref.min(axis=0)
            valid = span_cube > 1e-6
            if valid.any():
                scale = float((span_cube[valid] / span_ref[valid]).mean())
                if scale > 0.1:
                    radius /= scale
                    return radius
    return radius / 1.8897259886  # bohr -> angstrom fallback


def guess_cube_interpretation(header):
    """Best-effort identification from the cube comment lines."""
    if header is None:
        return "other"
    text = " ".join(header.get("comments", [])).lower()
    rules = (
        ("homo", ("homo", "highest occupied")),
        ("lumo", ("lumo", "lowest unoccupied")),
        ("spin_density", ("spin density", "spin density", "spindens")),
        ("density", ("electron density", "scf density", "total density",
                     "density")),
        ("fukui", ("fukui",)),
        ("orbital", ("orbital", "molecular orbital")),
        ("potential", ("potential", "mep", "esp")),
    )
    for label, needles in rules:
        if any(n in text for n in needles):
            return label
    return "other"


def suggest_isovalue(data_range):
    """A reasonable starting isovalue from the data range (editable)."""
    if not data_range:
        return 0.002
    max_abs = max(abs(data_range[0]), abs(data_range[1]))
    if max_abs < 1e-9:
        return 0.002
    exponent = math.floor(math.log10(max_abs))
    mantissa = max_abs / (10 ** exponent)
    value = round(mantissa * 0.1, 1) * (10 ** exponent)
    return value or 0.002


# ===========================================================================
# 6. Render settings, presets and defaults
# ===========================================================================

# VMD materials present in a stock 2.0 install (verified via `material list`);
# the AO* set is preferred for Tachyon ambient occlusion.
VMD_MATERIALS = [
    "AOChalky", "AOShiny", "AOEdgy", "Diffuse", "Goodsell", "Glossy",
    "Opaque", "HardPlastic", "BrushedMetal", "MetallicPastel", "Steel",
    "Translucent", "Edgy", "EdgyShiny", "EdgyGlass", "Ghost",
    "Glass1", "Glass2", "Glass3", "BlownGlass", "GlassBubble", "RTChrome",
]

REPRESENTATION_STYLES = [
    ("ball_and_stick", "Ball and Stick"),
    ("licorice", "Licorice"),
    ("cpk", "CPK / VDW (balls + bonds)"),
    ("lines", "Lines"),
    ("bonds", "Bonds"),
    ("space_filling", "Space Filling"),
    ("custom", "Custom"),
]

PRESETS = {
    # The reference rendering pipeline, reproduced bit-comparably
    # (verified by diffing generated Tachyon scenes): atoms AND
    # isosurfaces use the tweaked Glossy material (ambient 0.1,
    # diffuse 0.6, opacity 0.75, shininess 1.0 -> Phong Plastic 1,
    # Phong_size 1000: crisp specular highlights), Perspective camera,
    # black carbon skeleton (red O, white H), dedicated vivid-green Cl
    # rep (radius 1.75), lights 0/1/3, AA 24, Tachyon
    # -trans_raster3d -fullshade.
    "high_quality": {
        "label": "High Quality (default)",
        "engine": "auto",
        "style": "cpk",
        "sphere_scale": 0.80, "sphere_res": 22, "bond_radius": 0.30,
        "bond_res": 22,
        "color_mode": "element", "single_color": "#8899aa",
        "element_palette": "default",
        "bond_color": "element",
        "show_atoms": True, "show_bonds": True,
        "labels": "none", "label_size": 1.0, "label_color": "black",
        "material": "Glossy",
        "material_tweaks": {
            "Glossy": {"ambient": 0.10, "diffuse": 0.60,
                       "opacity": 0.75, "shininess": 1.0},
        },
        "ao": False, "ao_ambient": 0.8, "ao_direct": 0.4,
        "shadows": False, "depthcue": False,
        "lights": {"0": True, "1": True, "2": False, "3": True},
        "background": "white", "bg_color": "#ffffff",
        "projection": "Perspective",
        "width": 2000, "height": 2000, "format": "tga",
        "aasamples": 24, "transparent": False,
    },
    "publication_white": {
        "label": "Publication White",
        "engine": "auto",
        "style": "ball_and_stick",
        "sphere_scale": 0.30, "sphere_res": 40, "bond_radius": 0.15,
        "bond_res": 40,
        "color_mode": "element", "single_color": "#8899aa",
        "show_atoms": True, "show_bonds": True,
        "labels": "none", "label_size": 1.0, "label_color": "black",
        "material": "AOChalky",
        "ao": True, "ao_ambient": 0.85, "ao_direct": 0.35,
        "shadows": True, "depthcue": False,
        "background": "white", "bg_color": "#ffffff",
        "projection": "Orthographic",
        "width": 3000, "height": 3000, "format": "png",
        "aasamples": 12, "transparent": False,
    },
    "publication_dark": {
        "label": "Publication Dark",
        "engine": "auto",
        "style": "ball_and_stick",
        "sphere_scale": 0.30, "sphere_res": 40, "bond_radius": 0.15,
        "bond_res": 40,
        "color_mode": "element", "single_color": "#8899aa",
        "show_atoms": True, "show_bonds": True,
        "labels": "none", "label_size": 1.0, "label_color": "white",
        "material": "AOShiny",
        "ao": True, "ao_ambient": 0.80, "ao_direct": 0.45,
        "shadows": True, "depthcue": False,
        "background": "dark_gray", "bg_color": "#26262b",
        "projection": "Orthographic",
        "width": 3000, "height": 3000, "format": "png",
        "aasamples": 12, "transparent": False,
    },
    "high_gloss": {
        "label": "High Gloss",
        "engine": "auto",
        "style": "cpk",
        "sphere_scale": 0.35, "sphere_res": 64, "bond_radius": 0.16,
        "bond_res": 64,
        "color_mode": "element", "single_color": "#8899aa",
        "show_atoms": True, "show_bonds": True,
        "labels": "none", "label_size": 1.0, "label_color": "white",
        "material": "Glossy",
        "ao": False, "ao_ambient": 0.8, "ao_direct": 0.4,
        "shadows": True, "depthcue": False,
        "background": "gray", "bg_color": "#2b2b2b",
        "projection": "Perspective",
        "width": 3000, "height": 3000, "format": "png",
        "aasamples": 16, "transparent": False,
    },
    "scientific_matte": {
        "label": "Scientific Matte",
        "engine": "auto",
        "style": "ball_and_stick",
        "sphere_scale": 0.28, "sphere_res": 36, "bond_radius": 0.14,
        "bond_res": 36,
        "color_mode": "element", "single_color": "#8899aa",
        "show_atoms": True, "show_bonds": True,
        "labels": "none", "label_size": 1.0, "label_color": "black",
        "material": "Diffuse",
        "ao": False, "ao_ambient": 0.8, "ao_direct": 0.4,
        "shadows": True, "depthcue": False,
        "background": "white", "bg_color": "#ffffff",
        "projection": "Orthographic",
        "width": 3000, "height": 3000, "format": "png",
        "aasamples": 12, "transparent": False,
    },
    "minimal_clean": {
        "label": "Minimal Clean",
        "engine": "auto",
        "style": "licorice",
        "sphere_scale": 0.25, "sphere_res": 32, "bond_radius": 0.10,
        "bond_res": 32,
        "color_mode": "element", "single_color": "#4d4d4d",
        "show_atoms": True, "show_bonds": True,
        "labels": "none", "label_size": 1.0, "label_color": "black",
        "material": "AOChalky",
        "ao": False, "ao_ambient": 0.8, "ao_direct": 0.3,
        "shadows": False, "depthcue": False,
        "background": "white", "bg_color": "#ffffff",
        "projection": "Orthographic",
        "width": 2000, "height": 2000, "format": "png",
        "aasamples": 8, "transparent": False,
    },
}


def default_settings():
    """Baseline settings (preset-independent defaults, sane for chemistry)."""
    settings = dict(PRESETS["high_quality"])
    settings.update({
        "preset": "high_quality",
        "engine": "auto",
        "camera": "auto_fit",
        "match_moleditpy": True,
        "auto_center": True,
        "auto_fit": True,
        "custom_rot": [0.0, 0.0, 0.0],
        "geometry_source": "current",
        "external_path": "",
        "use_connectivity": True,
        "cube": {
            "enabled": False, "path": "", "interpretation": "auto",
            "show_pos": True, "show_neg": True,
            "iso_pos": 0.005, "iso_neg": -0.005,
            "pos_color": "#1e90ff", "neg_color": "#e81717",
            "opacity": 0.75, "material": "Glossy",
            "show_molecule": True,
            "map_path": "", "map_low": -0.03, "map_high": 0.0,
            "map_colormap": "BWR",
        },
        "output_root": "",
        "project_name": "",
    })
    return settings


# ===========================================================================
# 7. Tcl generation
# ===========================================================================

def _tcl_matrix(rotate_rows, scale, center):
    """molinfo matrix assignments applied to every loaded molecule
    (VMD stores the view transform per molecule)."""
    def mat(rows):
        # molinfo's set takes an attribute list and a VALUE LIST, so the
        # matrix itself must be nested one level deeper: { {row} {row} … }
        body = " ".join(
            "{" + " ".join(repr(float(v)) for v in row) + "}" for row in rows
        )
        return "{{ " + body + " }}"

    scale_rows = [
        [scale, 0, 0, 0], [0, scale, 0, 0], [0, 0, scale, 0], [0, 0, 0, 1]
    ]
    # VMD matrices are 4x4; pad the 3x3 rotation basis
    rotate_rows = [
        list(row) + [0.0] for row in rotate_rows
    ] + [[0.0, 0.0, 0.0, 1.0]]
    center_rows = [
        [1, 0, 0, center[0]], [0, 1, 0, center[1]], [0, 0, 1, center[2]],
        [0, 0, 0, 1],
    ]
    return (
        "foreach _m [molinfo list] {\n"
        f"  molinfo $_m set rotate_matrix {mat(rotate_rows)}\n"
        f"  molinfo $_m set scale_matrix {mat(scale_rows)}\n"
        f"  molinfo $_m set center_matrix {mat(center_rows)}\n"
        "}"
    )


def _hex_to_rgb01(hex_color):
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return (0.5, 0.5, 0.5)
    try:
        return tuple(int(hex_color[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return (0.5, 0.5, 0.5)


def _molecule_reps_tcl(settings, mol_var="mol"):
    """Representation commands for the molecular part.

    Two-rep model (atoms + bonds separately) so each can be hidden; the
    joint styles map onto the equivalent VMD single representations.
    """
    style = settings["style"]
    s_scale = float(settings["sphere_scale"])
    s_res = int(settings["sphere_res"])
    b_rad = float(settings["bond_radius"])
    b_res = int(settings["bond_res"])
    show_atoms = bool(settings["show_atoms"])
    show_bonds = bool(settings["show_bonds"])
    lines = []
    atom_colors = []
    if settings["color_mode"] == "element":
        atom_colors.append("mol color Element")
    else:
        r, g, b = _hex_to_rgb01(settings["single_color"])
        # colorID 30 is reassigned to the chosen RGB; verified pattern.
        atom_colors.append(f"color change rgb 30 {r:.4f} {g:.4f} {b:.4f}")
        atom_colors.append("mol color ColorID 30")

    # Uniform gray bonds (reference look): ColorID 2 is VMD's "gray",
    # re-toned here; the named color is unused by the element palettes.
    gray_bonds = settings.get("bond_color", "gray") == "gray"
    if gray_bonds:
        lines.append("# uniform gray bonds (reference look)")
        lines.append("color change rgb gray 0.5200 0.5200 0.5200")
        bond_colors = ["mol color ColorID 2"]
    else:
        bond_colors = list(atom_colors)

    def rep(style_str, colors):
        lines.append(f"mol representation {style_str}")
        lines.extend(colors)
        lines.append(f"mol addrep ${mol_var}")

    if style == "licorice":
        if show_atoms or show_bonds:
            rep(f"Licorice {b_rad:.4f} {b_res}", bond_colors)
    elif style == "lines":
        if show_bonds or show_atoms:
            rep("Lines", bond_colors)
    elif style == "space_filling":
        if show_atoms:
            rep(f"VDW {s_scale:.4f} {s_res}", atom_colors)
    elif style == "cpk":
        if gray_bonds:
            # CPK cannot color bonds independently: draw element-colored
            # spheres (VDW) + a uniform gray Bonds rep on top.
            if show_atoms:
                rep(f"VDW {s_scale:.4f} {s_res}", atom_colors)
            if show_bonds:
                rep(f"Bonds {b_rad:.4f} {b_res}", bond_colors)
        else:
            if show_atoms:
                rep(f"CPK {s_scale:.4f} {b_rad:.4f} {s_res} {b_res}",
                    atom_colors)
    else:  # ball_and_stick, bonds, custom -> atoms + bonds separately
        if show_atoms:
            rep(f"VDW {s_scale:.4f} {s_res}", atom_colors)
        if show_bonds:
            rep(f"Bonds {b_rad:.4f} {b_res}", bond_colors)

    # Chlorines get their real
    # vdW radius plus a dedicated vivid-green CPK rep drawn on top. The
    # rep MUST be scoped with mol modselect after being added — relying on
    # the default selection would paint the WHOLE molecule green.
    if (settings.get("element_palette", "default") == "default"
            and settings["color_mode"] == "element"):
        lines.extend([
            "# dedicated green Cl rep (as the installed .vmdrc does)",
            f"set clsel [atomselect ${mol_var} \"name CL Cl CLA ClA "
            "Cl1 CL1 Cl2 CL2 Cl3 CL3 Cl4 CL4\"]",
            "if {[$clsel num] > 0} {",
            "  $clsel set radius 1.75",
            "  set clrep [molinfo $mol get numreps]",
            "  mol addrep $mol",
            "  mol modselect $clrep $mol \"name CL Cl CLA ClA "
            "Cl1 CL1 Cl2 CL2 Cl3 CL3 Cl4 CL4\"",
            "  mol modstyle $clrep $mol CPK 0.900000 0.300000 "
            "22.000000 22.000000",
            "  mol modcolor $clrep $mol ColorID 7",
            "  mol modmaterial $clrep $mol Glossy",
            "}",
            "$clsel delete",
        ])
    return lines


def _labels_tcl(settings, natoms):
    if settings.get("labels", "none") == "none" or natoms <= 0:
        return []
    fmt = {
        "name": "%a", "index": "%i", "name_index": "%a %i",
    }.get(settings["labels"], "%a")
    lines = [
        f"label format Atoms {fmt}",
        f"label textsize {float(settings.get('label_size', 1.0)):.2f}",
        f"label textcolor {settings.get('label_color', 'black')}",
    ]
    for i in range(natoms):
        lines.append(f"label add Atoms 0/{i}")
    return lines


def _cube_tcl(settings, header, mol_var="mol"):
    """Volumetric block: same molecule when atom counts match, else its own
    molecule id.

    Two modes:
    * default — two Isosurface reps, positive BLUE / negative BLACK;
    * property map — an optional second cube colors the shape
      isosurface by its data (Volume coloring + BWR scale + min/max).

    Returns (lines, volume_mol_var): the Tcl variable holding the molecule
    the volumetric reps were added to ('' when the cube block is disabled).
    """
    cube = settings.get("cube") or {}
    if not cube.get("enabled") or not cube.get("path"):
        return [], ""
    lines = []
    same_mol = header is not None and header["natoms"] == _settings_natoms(
        settings
    )
    rel = "volumetric/" + Path(cube["path"]).name
    if same_mol:
        lines.append(f"mol addfile {rel} type cube waitfor all")
        volume_mol = mol_var
    else:
        lines.append(f"mol new {rel} type cube waitfor all")
        lines.append("set volmol [molinfo top get id]")
        volume_mol = "volmol"
        # drop the default Lines rep of the standalone cube molecule
        lines.append(f"mol delrep 0 ${volume_mol}")

    material = cube.get("material", "Glossy")
    opacity = float(cube.get("opacity", 1.0))

    map_path = cube.get("map_path") or ""
    if map_path:
        # Property-map mode: the property cube joins as volume dataset 1
        # and paints the shape isosurface through the color scale.
        map_rel = "volumetric/" + Path(map_path).name
        lines.append(f"mol addfile {map_rel} type cube waitfor all")
        lines.append(f"color scale method {cube.get('map_colormap', 'BWR')}")
        lines.append(
            f"mol representation Isosurface "
            f"{float(cube.get('iso_pos', 0.004)):.6f} 0 0 0 1 1"
        )
        lines.append("mol color Volume 1")
        lines.append(f"mol material {material}")
        lines.append(f"mol addrep ${volume_mol}")
        lines.append(
            f"mol scaleminmax ${volume_mol} "
            f"[expr [molinfo ${volume_mol} get numreps] - 1] "
            f"{float(cube.get('map_low', -0.03)):.6f} "
            f"{float(cube.get('map_high', 0.0)):.6f}"
        )
        if opacity < 0.999:
            lines.append(
                f"material change opacity {material} {opacity:.4f}"
            )
        return lines, volume_mol

    if opacity < 0.999:
        lines.append(f"material change opacity {material} {opacity:.4f}")

    def iso_rep(value, color_id, rgb):
        lines.append(
            f"color change rgb {color_id} {rgb[0]:.4f} {rgb[1]:.4f} "
            f"{rgb[2]:.4f}"
        )
        lines.append(
            f"mol representation Isosurface {value:.6f} 0 0 0 1 1"
        )
        lines.append(f"mol color ColorID {color_id}")
        lines.append(f"mol material {material}")
        lines.append(f"mol addrep ${volume_mol}")

    if cube.get("show_pos", True):
        iso_rep(
            float(cube.get("iso_pos", 0.005)), 31,
            _hex_to_rgb01(cube.get("pos_color", "#1e90ff")),
        )
    if cube.get("show_neg", True):
        iso_rep(
            float(cube.get("iso_neg", -0.005)), 32,
            _hex_to_rgb01(cube.get("neg_color", "#e81717")),
        )
    return lines, volume_mol


def _settings_natoms(settings):
    natoms = settings.get("_natoms")
    return natoms if natoms is not None else -1


# Element palettes. DEFAULT is the settled look: BLACK carbon skeleton,
# red O, white H and a dedicated vivid-green Cl rep (ColorID 7) with the
# real vdW radius, over the Glossy materials and framing of the
# reference pipeline. Applied only inside the scene, so nothing global
# is touched.
DEFAULT_PALETTE = [
    ("H", "white", None),
    ("C", "black", (0.0, 0.0, 0.0)),
    ("N", "blue", None),
    ("O", "red", None),
    ("F", "green", None),
    ("Cl", "green", (0.0, 1.0, 0.0)),
    ("Br", "red", (0.65, 0.16, 0.16)),
    ("I", "purple", (0.58, 0.0, 0.58)),
    ("S", "yellow", None),
    ("P", "orange", None),
]
ELEMENT_PALETTE = [  # black-skeleton variant
    ("H", "white", None),
    ("C", "black", (0.0, 0.0, 0.0)),
    ("N", "blue", None),
    ("O", "red", None),
    ("F", "green", None),
    ("Cl", "green", (0.0, 1.0, 0.0)),
    ("Br", "red", (0.65, 0.16, 0.16)),
    ("I", "purple", (0.58, 0.0, 0.58)),
    ("S", "yellow", None),
    ("P", "orange", None),
    ("B", "tan", None),
    ("Si", "tan", (0.94, 0.78, 0.63)),
    ("Na", "violet", None),
    ("K", "violet", None),
    ("Mg", "green2", None),
    ("Ca", "green2", None),
    ("Zn", "silver", None),
    ("Fe", "orange2", None),
    ("Cu", "orange2", (0.78, 0.5, 0.2)),
    ("Li", "green2", (0.8, 0.5, 1.0)),
]


def _element_colors_tcl(settings):
    """Element color overrides applied when coloring by Element."""
    palette = settings.get("element_palette", "default")
    entries = ELEMENT_PALETTE if palette == "black" else DEFAULT_PALETTE
    label = ("black carbon skeleton, vivid green Cl (default)"
             if palette != "black" else "black carbon skeleton")
    lines = [f"# element palette: {label}"]
    for element, name, rgb in entries:
        if rgb is not None:
            lines.append(
                f"color change rgb {name} {rgb[0]:.4f} {rgb[1]:.4f} "
                f"{rgb[2]:.4f}"
            )
        lines.append(f"color Element {element} {name}")
    return lines


def _material_tweaks_tcl(settings):
    """Material parameter overrides, {material: {param: value}} style."""
    tweaks = settings.get("material_tweaks") or {}
    lines = []
    for material, params in tweaks.items():
        if not isinstance(params, dict):
            continue
        for param, value in params.items():
            lines.append(
                f"material change {param} {material} {float(value):.4f}"
            )
    return lines


def _background_tcl(settings):
    mode = settings.get("background", "white")
    if mode == "white":
        return "color Display Background white"
    if mode == "gray":
        return ("color change rgb 34 0.1695 0.1695 0.1695\n"
                "color Display Background 34")
    if mode == "dark_gray":
        r, g, b = _hex_to_rgb01(settings.get("bg_color", "#26262b"))
    else:  # custom
        r, g, b = _hex_to_rgb01(settings.get("bg_color", "#ffffff"))
    return (
        f"color change rgb 33 {r:.4f} {g:.4f} {b:.4f}\n"
        "color Display Background 33"
    )


def build_scene_tcl(settings, camera, natoms, cube_header=None):
    """The editable scene script: everything visual, relative paths only.

    Molecule order: the molecular system is always molid 0 (labels and
    matrices below rely on it); volumetric data either joins molid 0 (cube
    atom count matches) or becomes its own molecule.
    """
    lines = [
        "# -------------------------------------------------------------",
        "# scene.tcl — generated by MoleditPy VMD-Tachyon Export",
        "# Edit freely, then re-render from the project folder with:",
        "#   vmd -dispdev none -e scene/render.tcl",
        "# (or re-render a GUI-edited snapshot with scene/render_edited.tcl)",
        "# -------------------------------------------------------------",
        "",
        "# --- geometry (exact MoleditPy coordinates + connectivity) ---",
    ]
    if settings.get("use_connectivity", True):
        lines.append("mol new geometry/molecule.psf type psf")
        lines.append(
            "mol addfile geometry/molecule.pdb type pdb waitfor all"
        )
    else:
        lines.append("mol new geometry/molecule.xyz type xyz waitfor all")
    lines.append("set mol 0")
    lines.append("")
    if settings.get("color_mode", "element") == "element":
        lines.extend(_element_colors_tcl(settings))
        lines.append("")
    # material tweaks come BEFORE any rep/cube opacity command so the
    # cube opacity slider always wins over preset defaults
    lines.extend(_material_tweaks_tcl(settings))
    lines.append("# --- representations ---")
    lines.append("mol delrep 0 $mol")
    lines.append(f"mol material {settings.get('material', 'AOChalky')}")
    lines.extend(_molecule_reps_tcl(settings))
    lines.append("set molreps [molinfo $mol get numreps]")
    lines.append("")
    lines.append("# --- atom labels (indices are 0-based, same order as "
                "MoleditPy) ---")
    lines.extend(_labels_tcl(settings, natoms))
    lines.append("")
    cube_lines, _volume_mol = _cube_tcl(settings, cube_header)
    if cube_lines:
        lines.append("# --- volumetric data (isosurfaces) ---")
        lines.extend(cube_lines)
        if not settings.get("cube", {}).get("show_molecule", True):
            lines.append("# molecule hidden, surface only")
            lines.append("for {set i 0} {$i < $molreps} {incr i} {"
                         "mol showrep $mol $i 0}")
        lines.append("")
    lines.append("# --- camera (center/rotate/scale; see README) ---")
    if camera.get("resetview"):
        # VMD's own auto-fit plus the reference camera recipe from
        # showcub.vmd's cub proc (distance -8 / height 10 = Zoom 1
        # framing in the generated Tachyon scene)
        lines.append("display resetview")
        lines.append("display distance -8.0")
        lines.append("display height 10")
    else:
        lines.append(_tcl_matrix(
            camera["rotate"], camera["scale"], camera["center"]
        ))
    projection = camera.get(
        "projection", settings.get("projection", "Orthographic")
    )
    lines.append(f"display projection {projection}")
    lines.append("display nearclip set 0.01")
    lines.append("display farclip set 100000.0")
    if settings.get("depthcue"):
        lines.append("display depthcue on")
        lines.append("display cuestart 2.0")
        lines.append("display cueend 8.0")
        lines.append("display cuedensity 0.35")
    else:
        lines.append("display depthcue off")
    lines.append("")
    lines.append("# --- lighting & materials ---")
    if settings.get("ao", True):
        lines.append("display ambientocclusion on")
        lines.append(
            f"display aoambient {float(settings.get('ao_ambient', 0.8)):.3f}"
        )
        lines.append(
            f"display aodirect {float(settings.get('ao_direct', 0.4)):.3f}"
        )
    else:
        lines.append("display ambientocclusion off")
    lines.append(
        "display shadows on" if settings.get("shadows") else
        "display shadows off"
    )
    for idx in ("0", "1", "2", "3"):
        state = "on" if settings.get("lights", {}).get(idx, False) else "off"
        lines.append(f"light {idx} {state}")
    lines.append("")
    lines.append("# --- background & chrome ---")
    lines.append(_background_tcl(settings))
    lines.append("axes location Off")
    return "\n".join(lines) + "\n"


def engine_choice(settings, installation):
    """Resolve 'auto' to a concrete engine; returns (engine, reason)."""
    engine = settings.get("engine", "auto")
    if engine == "internal":
        return "internal", "built-in Tachyon (resolution rounded to a " \
            "multiple of 16 by VMD)"
    if engine == "tachyon":
        if installation and installation.has_external_tachyon:
            return "tachyon", "external Tachyon (exact resolution)"
        return "internal", "external Tachyon requested but not found; " \
            "using built-in engine"
    # auto
    if installation and installation.has_external_tachyon:
        return "tachyon", "external Tachyon (exact resolution)"
    return "internal", "external Tachyon not found; built-in engine"


def _render_commands(engine, settings, aasamples):
    """Tcl render call(s) for the chosen engine (relative paths)."""
    if engine == "tachyon":
        return [f"render Tachyon tachyon/scene.dat"]
    width, height = settings.get("width", 3000), settings.get("height", 3000)
    return [
        f"render TachyonInternal output/render.tga -aasamples {aasamples}"
    ]


def build_wrapper_scripts(settings, engine, aasamples):
    """build_scene.tcl / render.tcl / render_edited.tcl contents.

    Every script ends with a plain `exit`: without it VMD stays in its
    Tcl command loop after the last command and a caller that keeps
    stdin open (e.g. QProcess) would wait forever.
    """
    render_calls = "\n".join(_render_commands(engine, settings, aasamples))
    build = (
        "# Initial build: reconstruct the scene, snapshot it, render.\n"
        "# Run from the project folder "
        "(vmd -dispdev none -e scene/build_scene.tcl)\n"
        "if {[catch {source scene/scene.tcl} e]} {\n"
        "  puts \"ERROR building scene: $e\"\n"
        "  exit 1\n"
        "}\n"
        "save_state scene/scene.vmd\n"
        f"{render_calls}\n"
        "exit\n"
    )
    render = (
        "# Re-render from the editable scene (does NOT touch scene.vmd,\n"
        "# so GUI edits saved there survive). Run from the project folder:\n"
        "#   vmd -dispdev none -e scene/render.tcl\n"
        "if {[catch {source scene/scene.tcl} e]} {\n"
        "  puts \"ERROR building scene: $e\"\n"
        "  exit 1\n"
        "}\n"
        f"{render_calls}\n"
        "exit\n"
    )
    edited = (
        "# Re-render the snapshot scene/scene.vmd (e.g. after editing it\n"
        "# in the VMD GUI and saving over it with File > Save State).\n"
        "# scene.vmd stores absolute paths from this machine.\n"
        "play scene/scene.vmd\n"
        f"{render_calls}\n"
        "exit\n"
    )
    return build, render, edited


# ===========================================================================
# 8. Project folder management
# ===========================================================================

PROJECT_SUBDIRS = ("geometry", "volumetric", "scene", "tachyon", "output",
                   "logs")


def default_output_root(context=None):
    """VMD_Renders next to the current file, else the user's home."""
    if context is not None:
        current = context.get_current_file()
        if current:
            return str(Path(current).parent / "VMD_Renders")
    return str(Path.home() / "VMD_Renders")


def next_project_dir(output_root, project_name="molecule"):
    """molecule_001, molecule_002 ... never overwrite an existing folder."""
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    base = "".join(c if c.isalnum() or c in "-_" else "_"
                   for c in project_name.strip()) or "molecule"
    index = 1
    while (root / f"{base}_{index:03d}").exists():
        index += 1
    project = root / f"{base}_{index:03d}"
    for sub in PROJECT_SUBDIRS:
        (project / sub).mkdir(parents=True, exist_ok=True)
    return project


def write_project_files(project, settings, installation, engine,
                        engine_reason, scene_tcl, build_tcl, render_tcl,
                        render_edited_tcl, natoms, cube_header,
                        cube_range, molecule_name, tachyon_command):
    """All bookkeeping files of a render project."""
    (project / "scene" / "scene.tcl").write_text(scene_tcl, encoding="utf-8")
    (project / "scene" / "build_scene.tcl").write_text(
        build_tcl, encoding="utf-8"
    )
    (project / "scene" / "render.tcl").write_text(
        render_tcl, encoding="utf-8"
    )
    (project / "scene" / "render_edited.tcl").write_text(
        render_edited_tcl, encoding="utf-8"
    )
    (project / "scene" / "render_settings.json").write_text(
        json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    manifest = {
        "generator": f"{PLUGIN_NAME} v{PLUGIN_VERSION}",
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "molecule": molecule_name,
        "natoms": natoms,
        "vmd": installation.as_dict() if installation else {},
        "engine": engine,
        "engine_reason": engine_reason,
        "render_command": tachyon_command,
        "resolution": [settings.get("width"), settings.get("height")],
        "format": settings.get("format"),
        "camera": settings.get("camera"),
        "match_moleditpy_orientation": settings.get("match_moleditpy"),
        "preset": settings.get("preset"),
        "files": {
            "geometry": ["geometry/molecule.psf", "geometry/molecule.pdb",
                         "geometry/molecule.xyz"],
            "volumetric": (
                ["volumetric/" + Path(settings["cube"]["path"]).name]
                if settings.get("cube", {}).get("enabled") else []
            ),
            "scene": ["scene/scene.tcl", "scene/build_scene.tcl",
                      "scene/render.tcl", "scene/render_edited.tcl",
                      "scene/render_settings.json", "scene/scene.vmd"],
            "tachyon": ["tachyon/scene.dat"] if engine == "tachyon" else [],
            "output": ["output/"],
            "logs": ["logs/vmd_render.log"],
        },
        "cube": {},
    }
    if cube_header is not None:
        manifest["cube"] = {
            "source": settings.get("cube", {}).get("path", ""),
            "interpretation": settings.get("cube", {}).get(
                "interpretation", "auto"
            ),
            "header_comments": cube_header.get("comments"),
            "natoms": cube_header.get("natoms"),
            "dims": cube_header.get("dims"),
            "data_range": cube_range,
            "isovalues": [settings.get("cube", {}).get("iso_pos"),
                          settings.get("cube", {}).get("iso_neg")],
            "property_map": settings.get("cube", {}).get("map_path", ""),
            "map_scale": [settings.get("cube", {}).get("map_low"),
                          settings.get("cube", {}).get("map_high")],
            "map_colormap": settings.get("cube", {}).get("map_colormap",
                                                         "BWR"),
        }
    (project / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    tachyon_note = (
        f"    tachyon/scene.dat   (external engine scene; re-render with:\n"
        f"      {tachyon_command})\n"
        if engine == "tachyon" else
        "    tachyon/            (empty: the built-in engine renders "
        "directly)\n"
    )
    readme = f"""# VMD render project — {project.name}

Generated by MoleditPy {PLUGIN_NAME} v{PLUGIN_VERSION}
Molecule: {molecule_name} ({natoms} atoms)
Engine: {engine} ({engine_reason})

## Layout
    geometry/           exact MoleditPy coordinates + connectivity
    volumetric/         cube data used by this render, if any
    scene/scene.tcl     THE editable scene (relative paths; portable)
    scene/scene.vmd     VMD state snapshot (absolute paths; machine-local)
    scene/render.tcl    re-render from scene.tcl
    scene/render_edited.tcl  re-render from scene.vmd
{tachyon_note}    output/             rendered images
    logs/               full process transcript
    render_settings.json / manifest.json   parameters and provenance

## Open and edit in VMD (GUI)

    cd {project}
    {installation.vmd_path if installation else "vmd"} -e scene/scene.vmd

Rotate, change materials, representations, isosurfaces, lighting ...
then File > Save State... over scene/scene.vmd.

## Re-render without MoleditPy

From this folder:

    {(installation.vmd_path if installation else 'vmd')} -dispdev none -e scene/render.tcl

After editing scene.vmd in the GUI:

    {(installation.vmd_path if installation else 'vmd')} -dispdev none -e scene/render_edited.tcl

{'    ' + tachyon_command + '\n' if engine == 'tachyon' else ''}## Notes

* scene.tcl keeps every file reference relative to this folder; move the
  whole folder freely. scene.vmd stores absolute paths of this machine.
* Atom indices in labels are 0-based and identical to MoleditPy's order
  (PDB/PSF serial = index + 1).
* Connectivity comes from MoleditPy's topology (PSF); VMD never re-guesses
  bonds by distance, which matters for radicals and transition states.
"""
    (project / "README.md").write_text(readme, encoding="utf-8")
    return manifest


# ===========================================================================
# 9. Image post-processing (TGA -> PNG/TIFF, transparent keying)
# ===========================================================================

def decode_tga_bytes(data):
    """Parse an uncompressed 24/32-bit TGA (Tachyon output) into
    (height, width, channels) uint8 numpy array. Pure numpy fallback for
    hosts without Pillow."""
    import numpy as np

    if len(data) < 18:
        raise ValueError("TGA too short")
    id_length = data[0]
    image_type = data[2]
    if image_type not in (2, 3):
        raise ValueError(f"unsupported TGA type {image_type} (RLE/palette)")
    width = int.from_bytes(data[12:16][:2], "little")
    height = int.from_bytes(data[14:16], "little")
    bits = data[16]
    descriptor = data[17]
    if bits not in (24, 32):
        raise ValueError(f"unsupported TGA bit depth {bits}")
    offset = 18 + id_length
    channels = 3 if bits == 24 else 4
    expected = width * height * channels
    raw = np.frombuffer(data[offset:offset + expected], dtype=np.uint8)
    if raw.size < expected:
        raise ValueError("truncated TGA pixel data")
    image = raw.reshape(height, width, channels).copy()
    image = image[..., ::-1]  # BGR(A) -> RGB(A)
    if not descriptor & 0x20:  # bottom-up storage -> flip
        image = image[::-1, :]
    return image


def tga_to_image(tga_path):
    """Load a Tachyon TGA as a PIL image when Pillow exists, else build a
    QImage from decoded bytes. Returns ('pil', img) or ('qimage', img)."""
    data = Path(tga_path).read_bytes()

    try:
        from PIL import Image  # type: ignore

        import io

        return "pil", Image.open(io.BytesIO(data)).convert("RGB")
    except ImportError:
        pass

    from PySide6.QtGui import QImage

    array = decode_tga_bytes(data)
    height, width = array.shape[:2]
    image = QImage(
        array.data(), width, height, 3 * width, QImage.Format.Format_RGB888
    )
    return "qimage", image.copy()  # copy: array buffer must outlive uses


def chroma_key_background(rgb_array, bg_rgb, tol_low=6, tol_high=28):
    """Turn the (near-)background color into alpha with edge de-fringing.

    rgb_array: HxWx3 uint8; bg_rgb: (r, g, b). Returns HxWx4 uint8.
    Pixels within tol_low (Euclidean RGB distance) count as background
    (alpha 0) — Tachyon's TGA quantization differs from the requested
    color by ±1 unit; tol_high marks solid content. Between them the
    alpha ramps and edge colors are un-mixed from the background so
    anti-aliased halos vanish on any backdrop.
    """
    import numpy as np

    arr = rgb_array.astype(np.float32)
    bg = np.array(bg_rgb, dtype=np.float32)
    distance = np.sqrt(((arr - bg) ** 2).sum(axis=2))
    alpha = np.clip(
        (distance - tol_low) / max(1.0, tol_high - tol_low), 0.0, 1.0
    )

    soft = (alpha < 1.0) & (alpha > 0.0)
    unmix = arr.copy()
    a = alpha[soft][:, None]
    unmix[soft] = np.clip(
        (arr[soft] - bg[None, :] * (1.0 - a)) / np.maximum(a, 1e-3), 0, 255
    )
    out = np.dstack([unmix, alpha * 255.0]).astype(np.uint8)
    return out


def save_converted_images(tga_path, out_dir, stem, image_format,
                          transparent, bg_rgb):
    """Produce the final image files from Tachyon's TGA.

    Returns (files_created, notes). With image_format == "tga" the native
    Tachyon output IS the deliverable (no re-encode); "png"/"tiff"
    additionally derive lossless conversions from it.
    """
    notes = []
    created = []
    out_dir = Path(out_dir)

    kind = image = None
    if image_format == "tga" and not transparent:
        created.append(str(Path(tga_path)))
        notes.append(
            "native Tachyon TGA kept as the deliverable (no re-encode)"
        )
        return created, notes

    kind, image = tga_to_image(tga_path)
    if image_format == "tga":
        created.append(str(Path(tga_path)))  # native master first
    elif kind == "pil":
        png_path = out_dir / f"{stem}.png"
        image.save(png_path, format="PNG")
        created.append(str(png_path))
        if image_format == "tiff":
            try:
                tif_path = out_dir / f"{stem}.tiff"
                image.save(tif_path, format="TIFF")
                created.append(str(tif_path))
            except (ValueError, OSError) as e:
                notes.append(f"TIFF export failed: {e}")
    else:
        png_path = out_dir / f"{stem}.png"
        if not image.save(str(png_path), "PNG"):
            raise OSError(f"could not write {png_path}")
        created.append(str(png_path))
        if image_format == "tiff":
            notes.append("TIFF needs Pillow; only PNG was written")

    if transparent:
        import numpy as np

        if kind == "pil":
            rgb = np.array(image.convert("RGB"))
        else:
            rgb = decode_tga_bytes(Path(tga_path).read_bytes())[..., :3]
        keyed = chroma_key_background(rgb, bg_rgb)
        trans_path = out_dir / f"{stem}_trans.png"
        try:
            from PIL import Image  # type: ignore

            Image.fromarray(keyed, "RGBA").save(trans_path, format="PNG")
        except ImportError:
            from PySide6.QtGui import QImage

            h, w = keyed.shape[:2]
            qimg = QImage(
                keyed.copy(), w, h, 4 * w, QImage.Format.Format_RGBA8888
            )
            qimg.save(str(trans_path), "PNG")
        created.append(str(trans_path))
        notes.append(
            "transparent background is post-processed (this Tachyon build "
            "writes no alpha channel): *_trans.png keys out the exact "
            "background color with de-fringed edges"
        )
    return created, notes


# ===========================================================================
# 10. Render preparation (MoleditPy data -> project folder)
# ===========================================================================

class PreparationError(Exception):
    """Raised with a user-facing message when a render cannot be prepared."""


def _hex_to_rgb255(hex_color):
    r, g, b = _hex_to_rgb01(hex_color)
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def resolve_background_rgb(settings):
    mode = settings.get("background", "white")
    if mode == "white":
        return (255, 255, 255)
    if mode == "gray":
        return (43, 43, 43)  # neutral gray, matches the reference renders
    if mode == "dark_gray":
        return _hex_to_rgb255(settings.get("bg_color", "#26262b"))
    return _hex_to_rgb255(settings.get("bg_color", "#2b2b2b"))


def prepare_project(context, settings, installation, log):
    """Validate everything and write the render project folder.

    Returns a dict describing the prepared render (paths, engine, camera,
    natoms). Raises PreparationError with a clear message on any problem.
    """
    settings = dict(settings)
    engine, engine_reason = engine_choice(settings, installation)
    settings["engine"] = engine

    # ---- molecule source -------------------------------------------------
    geometry_source = settings.get("geometry_source", "current")
    molecule_name = "molecule"
    mol = None
    if context is not None:
        mol = context.current_mol

    if geometry_source == "external":
        ext_path = settings.get("external_path", "")
        if not ext_path or not Path(ext_path).is_file():
            raise PreparationError(
                f"External geometry file not found: {ext_path or '(none)'}"
            )
        symbols, positions, bonds, note = load_external_geometry(
            Path(ext_path), mol
        )
        if symbols is None:
            raise PreparationError(note)
        if note:
            log(f"[geometry] {note}")
        molecule_name = Path(ext_path).stem
    else:
        if mol is None or mol.GetNumAtoms() == 0:
            raise PreparationError(
                "No molecule loaded. Create or load a molecule first."
            )
        if mol.GetNumConformers() == 0:
            raise PreparationError(
                "The molecule has no 3D coordinates "
                "(generate a 3D structure first)."
            )
        symbols, positions, bonds = mol_atom_data(mol)
        molecule_name = (mol.GetProp("_Name") if mol.HasProp("_Name")
                         and mol.GetProp("_Name") else "molecule")

    natoms = len(symbols)
    if natoms == 0:
        raise PreparationError("The molecule has no atoms.")
    for x, y, z in positions:
        if not all(math.isfinite(c) for c in (x, y, z)):
            raise PreparationError(
                "The molecule contains non-finite 3D coordinates."
            )
    settings["_natoms"] = natoms

    # ---- volumetric data --------------------------------------------------
    cube_header, cube_range = None, None
    cube = dict(settings.get("cube") or {})
    if not cube.get("path"):
        # Auto-association: when the molecule itself comes from a cube file
        # (opened in MoleditPy or loaded as external geometry), that same
        # file IS the volumetric data — the isosurface is expected without
        # the user re-entering anything in the Volumetric tab.
        candidates = []
        if geometry_source == "external":
            candidates.append(settings.get("external_path", ""))
        if context is not None:
            candidates.append(context.get_current_file() or "")
        for candidate in candidates:
            if not candidate or not Path(candidate).is_file():
                continue
            candidate_header = parse_cube_header(candidate)
            if candidate_header is None:
                continue
            if candidate_header["natoms"] != natoms:
                log(
                    f"[cube] {Path(candidate).name} describes "
                    f"{candidate_header['natoms']} atoms but the molecule "
                    f"has {natoms}: not auto-associated"
                )
                continue
            cube["enabled"] = True
            cube["path"] = candidate
            cube["interpretation"] = guess_cube_interpretation(
                candidate_header
            )
            log(
                f"[cube] auto-associated from the loaded file: {candidate} "
                "(same system — isosurface enabled)"
            )
            break
    if cube.get("enabled"):
        cube_path = cube.get("path", "")
        if not cube_path or not Path(cube_path).is_file():
            raise PreparationError(
                f"Cube file not found: {cube_path or '(none)'} "
                "(accepted: .cub, .cube, .gz)"
            )
        cube_header = parse_cube_header(cube_path)
        if cube_header is None:
            raise PreparationError(
                f"Not a readable Gaussian cube file: {cube_path}"
            )
        cube_range = cube_data_range(cube_path, cube_header)
        if cube_range is None:
            raise PreparationError(
                f"Could not read the data block of {cube_path}"
            )
        if cube.get("interpretation", "auto") == "auto":
            cube["interpretation"] = guess_cube_interpretation(cube_header)
        map_path = cube.get("map_path") or ""
        if map_path:
            if not Path(map_path).is_file():
                raise PreparationError(
                    f"Property-map cube not found: {map_path}"
                )
            map_header = parse_cube_header(map_path)
            if map_header is None:
                raise PreparationError(
                    f"Not a readable property-map cube: {map_path}"
                )
            if map_header["natoms"] != cube_header["natoms"]:
                raise PreparationError(
                    "The property map must describe the same system as the "
                    "shape cube "
                    f"({map_header['natoms']} vs {cube_header['natoms']} "
                    "atoms)"
                )
            if not cube.get("map_range_set"):
                map_range = cube_data_percentiles(map_path, map_header)
                if map_range:
                    cube["map_low"] = round(map_range[0], 6)
                    cube["map_high"] = round(map_range[1], 6)
                    cube["map_range_set"] = True
                    log(
                        "[cube] property map range "
                        f"[{cube['map_low']:.4g}, {cube['map_high']:.4g}] "
                        "auto-filled from percentiles (editable in the "
                        "Volumetric tab)"
                    )
        settings["cube"] = cube

    # ---- camera -----------------------------------------------------------
    matched = False
    camera = None
    if settings.get("match_moleditpy") and context is not None:
        plotter = context.plotter
        if plotter is not None and getattr(plotter, "camera", None):
            try:
                size = getattr(plotter, "window_size", None) or (1024, 1024)
                camera = vtk_camera_to_vmd(plotter.camera, tuple(size))
                matched = True
                log(
                    "[camera] matched MoleditPy orientation "
                    f"({camera['projection'].lower()}, scale "
                    f"{camera['scale']:.4f})"
                )
            except (ValueError, RuntimeError) as e:
                log(f"[camera] could not match orientation ({e}); "
                    "using the selected preset orientation")
    if camera is None:
        # Content-aware framing: when an isosurface is rendered, fit to
        # its real extent (from the cube) instead of the bare atom
        # skeleton, so large surfaces are never cropped by the camera.
        fit_radius = None
        if cube.get("enabled") and cube.get("path"):
            iso = min(
                abs(float(cube.get("iso_pos", 0.005))),
                abs(float(cube.get("iso_neg", -0.005))),
            )
            fit_radius = cube_isosurface_radius(
                cube["path"], iso, cube_header,
                reference_positions=positions,
            )
            if fit_radius:
                log(f"[camera] fitting to isosurface extent "
                    f"(radius {fit_radius:.2f})")
        camera = preset_camera(
            settings.get("camera", "auto_fit"),
            positions,
            "Orthographic"
            if settings.get("projection", "orthographic").lower().startswith(
                "o")
            else "Perspective",
            auto_center=settings.get("auto_center", True),
            auto_fit=settings.get("auto_fit", True),
            custom_rot=settings.get("custom_rot", (0, 0, 0)),
            fit_margin=float(settings.get("fit_margin", 1.15)),
            fit_radius=fit_radius,
        )
    settings["match_moleditpy"] = matched

    # ---- project folder ---------------------------------------------------
    output_root = settings.get("output_root") or default_output_root(context)
    settings["output_root"] = output_root
    base_name = settings.get("project_name") or molecule_name
    project = next_project_dir(output_root, base_name)
    log(f"[project] {project}")

    # geometry files
    (project / "geometry" / "molecule.xyz").write_text(
        write_xyz(symbols, positions, comment=molecule_name),
        encoding="utf-8",
    )
    (project / "geometry" / "molecule.pdb").write_text(
        write_pdb(symbols, positions), encoding="utf-8"
    )
    (project / "geometry" / "molecule.psf").write_text(
        write_psf(symbols, positions, bonds if settings.get(
            "use_connectivity", True) else []),
        encoding="utf-8",
    )

    # volumetric copy
    if cube.get("enabled"):
        shutil.copy2(
            cube["path"], project / "volumetric" / Path(cube["path"]).name
        )
        if cube.get("map_path"):
            shutil.copy2(
                cube["map_path"],
                project / "volumetric" / Path(cube["map_path"]).name,
            )

    # scripts
    scene_tcl = build_scene_tcl(settings, camera, natoms, cube_header)
    aasamples = int(settings.get("aasamples", 12))
    build_tcl, render_tcl, edited_tcl = build_wrapper_scripts(
        settings, engine, aasamples
    )

    tachyon_command = ""
    if engine == "tachyon":
        width = int(settings.get("width", 3000))
        height = int(settings.get("height", 3000))
        aasamples = int(settings.get("aasamples", 12))
        tachyon_bin = installation.tachyon_path
        tachyon_command = (
            f"{tachyon_bin} tachyon/scene.dat -o output/render.tga "
            f"-res {width} {height} -aasamples {aasamples} "
            f"-trans_raster3d -fullshade"
        )

    write_project_files(
        project, settings, installation, engine, engine_reason,
        scene_tcl, build_tcl, render_tcl, edited_tcl, natoms,
        cube_header, cube_range, molecule_name, tachyon_command,
    )

    return {
        "project": project,
        "settings": settings,
        "engine": engine,
        "engine_reason": engine_reason,
        "natoms": natoms,
        "camera": camera,
        "molecule_name": molecule_name,
        "molecule_title": f"{molecule_name} ({natoms} atoms)",
        "tachyon_command": tachyon_command,
        "log_path": project / "logs" / "vmd_render.log",
    }


def vmd_render_size(width, height):
    """VMD rounds the internal buffer to multiples of 16 (verified: -size
    1400 1000 renders 1408x1008). Pre-round ourselves and keep the aspect
    close to the requested one so the external -res override (exact)
    introduces no visible distortion."""
    def up16(v):
        return max(16, int(math.ceil(v / 16.0)) * 16)

    if width <= 0 or height <= 0:
        return 1024, 1024
    aspect = width / height
    h16 = up16(height)
    w16 = up16(round(h16 * aspect))
    return w16, h16


# ===========================================================================
# 11. Asynchronous render runner (QProcess, GUI stays responsive)
# ===========================================================================

class RenderRunner:
    """Two-phase QProcess pipeline: VMD (build+render scene) then the
    external Tachyon binary when needed. Only its own child processes are
    ever touched; everything is logged to the project log file."""

    def __init__(self, parent, installation, prepared, on_log, on_phase,
                 on_done):
        from PySide6.QtCore import QProcess, QProcessEnvironment

        self._QProcess = QProcess
        self.parent = parent
        self.installation = installation
        self.prepared = prepared
        self._on_log = on_log
        self._on_phase = on_phase
        self._on_done = on_done
        self._process = None
        self._log_handle = None
        self._cancelled = False
        self._started = time.monotonic()
        self.project = prepared["project"]
        self.settings = prepared["settings"]
        self.engine = prepared["engine"]
        try:
            self._log_handle = open(
                self.project / "logs" / "vmd_render.log", "a",
                encoding="utf-8",
            )
        except OSError as e:
            self._on_log(f"[log] cannot open log file: {e}")
        self._env = QProcessEnvironment.systemEnvironment()
        # VMD's launcher expects a sane HOME for its dotfiles
        self._env.insert("HOME", str(Path.home()))

    # ------------------------------------------------------------- helpers
    def log(self, text):
        for line in text.splitlines() or (text,):
            self._on_log(line)
            if self._log_handle is not None:
                try:
                    self._log_handle.write(line + "\n")
                    self._log_handle.flush()
                except OSError:
                    pass

    def is_running(self):
        return self._process is not None

    # --------------------------------------------------------------- start
    def start(self):
        width = int(self.settings.get("width", 3000))
        height = int(self.settings.get("height", 3000))
        size_w, size_h = vmd_render_size(width, height)
        engine = self.engine
        resetview = bool(self.prepared["camera"].get("resetview"))

        phase = (
            "VMD: building scene"
            if engine == "tachyon"
            else f"VMD: building + rendering (TachyonInternal, "
                 f"{size_w}x{size_h})"
        )
        self.log(f"[render] engine: {engine} — {self.prepared['engine_reason']}")
        if engine == "internal":
            self.log(
                f"[render] note: built-in engine output is {size_w}x{size_h} "
                f"(VMD rounds to multiples of 16); the external engine "
                f"renders exact {width}x{height}"
            )
        self._on_phase(phase)

        # resetview + external engine: reproduce the reference pipeline
        # exactly — VMD's DEFAULT portrait window (no -size), display
        # resetview (Zoom 1), then Tachyon -res for the final resolution.
        # The default-window framing is what gives the reference renders
        # their large, wide-perspective look.
        if resetview and engine == "tachyon":
            args = ["-dispdev", "none", "-e", "scene/build_scene.tcl"]
            self.log("[render] camera: VMD default window + resetview "
                     "(reference framing)")
        else:
            args = ["-dispdev", "none", "-size", str(size_w), str(size_h),
                    "-e", "scene/build_scene.tcl"]
        self._run_process(
            self.installation.vmd_path, args, str(self.project),
            self._vmd_finished,
        )

    def _vmd_finished(self, exit_code, message):
        if self._cancelled:
            return
        if exit_code != 0:
            self._finish(False, f"VMD failed: {message}")
            return
        if self.engine != "tachyon":
            tga = self.project / "output" / "render.tga"
            if not tga.is_file() or tga.stat().st_size < 100:
                self._finish(False, "VMD produced no image "
                                    "(check scene/log for Tcl errors)")
                return
            self._postprocess(tga)
            return
        dat = self.project / "tachyon" / "scene.dat"
        if not dat.is_file():
            self._finish(False, "VMD did not write tachyon/scene.dat")
            return
        width = int(self.settings.get("width", 3000))
        height = int(self.settings.get("height", 3000))
        aasamples = int(self.settings.get("aasamples", 12))
        self._on_phase(
            f"Tachyon: ray-tracing {width}x{height} "
            f"(AA {aasamples} samples)"
        )
        # -trans_raster3d + -fullshade: borisiori's re_renderizar.sh
        # post-render flags (Raster3D angle-based transparency for the
        # translucent Glossy lobes, full shading quality)
        args = [
            "tachyon/scene.dat", "-o", "output/render.tga",
            "-res", str(width), str(height),
            "-aasamples", str(aasamples),
            "-trans_raster3d", "-fullshade",
        ]
        self._run_process(
            self.installation.tachyon_path, args, str(self.project),
            self._tachyon_finished,
        )

    def _tachyon_finished(self, exit_code, message):
        if self._cancelled:
            return
        tga = self.project / "output" / "render.tga"
        if exit_code != 0:
            self._finish(False, f"Tachyon failed: {message}")
            return
        if not tga.is_file() or tga.stat().st_size < 100:
            self._finish(False, "Tachyon produced no image file")
            return
        self._postprocess(tga)

    # ---------------------------------------------------------- conversion
    def _postprocess(self, tga_path):
        from PySide6.QtCore import QTimer

        self._on_phase("Converting image (lossless)")
        QTimer.singleShot(0, self._convert_now)

    def _convert_now(self):
        try:
            bg = resolve_background_rgb(self.settings)
            created, notes = save_converted_images(
                tga_path=self.project / "output" / "render.tga",
                out_dir=self.project / "output",
                stem="render",
                image_format=self.settings.get("format", "png"),
                transparent=bool(self.settings.get("transparent")),
                bg_rgb=bg,
            )
            for note in notes:
                self.log(f"[image] {note}")
            self._finish(True, "", files=created)
        except (ValueError, OSError) as e:
            self._finish(False, f"image conversion failed: {e}")

    # ------------------------------------------------------------ internals
    def _run_process(self, program, args, workdir, finished_cb):
        process = self._QProcess(self.parent)
        process.setProcessChannelMode(
            self._QProcess.ProcessChannelMode.MergedChannels
        )
        process.setProcessEnvironment(self._env)
        process.setWorkingDirectory(workdir)
        process.readyRead.connect(
            lambda p=process: self._read_output(p)
        )
        process.finished.connect(
            lambda code, status, p=process, cb=finished_cb:
            self._process_done(p, code, status, cb)
        )
        process.errorOccurred.connect(
            lambda err, p=process, cb=finished_cb:
            self._process_error(p, err, cb)
        )
        self._process = process
        self.log(f"[cmd] {program} {' '.join(args)}")
        process.start(program, args)
        # Signal EOF on stdin: even with `exit` in every generated script,
        # a VMD left waiting for Tcl input must never outlive its caller.
        process.closeWriteChannel()

    def _read_output(self, process):
        try:
            chunk = bytes(process.readAll())
        except RuntimeError:
            return
        text = chunk.decode("utf-8", errors="replace")
        keep = [
            line for line in text.splitlines()
            if line.startswith(("Info)", "Error)", "ERROR)", "Warning",
                                "[cmd]", "[render]", "[image]", "[camera]",
                                "[project]", "[geometry]", "Scene",
                                "Rendering", "Ray", "Image", "Preprocess"))
            or "error" in line.lower()
        ]
        if keep:
            self.log("\n".join(keep))

    def _process_done(self, process, code, status, cb):
        if self._process is process:
            self._process = None
        try:
            chunk = bytes(process.readAll())
        except RuntimeError:
            chunk = b""
        if chunk:
            text = chunk.decode("utf-8", errors="replace")
            errors = [l for l in text.splitlines()
                      if "error" in l.lower() or l.startswith("ERROR)")]
            if errors:
                self.log("\n".join(errors[-5:]))
        process.deleteLater()
        message = f"exit code {code}" + (
            f" ({status.name})" if hasattr(status, "name") else ""
        )
        cb(code, message)

    def _process_error(self, process, error, cb):
        names = {
            0: "failed to start (bad executable path?)",
            1: "crashed",
            2: "timed out",
            4: "write error",
        }
        text = names.get(int(error), f"error {int(error)}")
        if int(error) == 0:  # FailedToStart: finished() will NOT follow
            if self._process is process:
                self._process = None
            process.deleteLater()
            self._finish(False, f"process {text}")

    # --------------------------------------------------------------- finish
    def _finish(self, ok, message, files=None):
        elapsed = time.monotonic() - self._started
        self.log(
            f"[render] {'finished' if ok else 'FAILED'} "
            f"({elapsed:.1f} s){' — ' + message if message else ''}"
        )
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None
        result = {
            "ok": ok,
            "message": message,
            "elapsed": elapsed,
            "project": self.project,
            "files": files or [],
            "engine": self.engine,
            "resolution": (
                self.settings.get("width"), self.settings.get("height")
            ),
        }
        try:
            self._on_done(result)
        except RuntimeError:  # dialog destroyed mid-signal
            pass

    # ---------------------------------------------------------------- stop
    def cancel(self):
        """Terminate only the process this runner started."""
        self._cancelled = True
        process, self._process = self._process, None
        if process is not None:
            process.terminate()
            from PySide6.QtCore import QTimer

            def kill():
                if process.state() != self._QProcess.ProcessState.NotRunning:
                    process.kill()

            QTimer.singleShot(3000, kill)
        if self._log_handle is not None:
            try:
                self._log_handle.write("[render] cancelled by user\n")
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None


# ===========================================================================
# 12. Export dialog
# ===========================================================================

class VmdTachyonDialog:
    """Constructed lazily inside _show_dialog; the class body is defined in
    _build_dialog_class so importing this plugin never touches Qt."""


def _build_dialog_class():
    from PySide6.QtCore import QSize, Qt
    from PySide6.QtGui import QColor, QFont, QPixmap
    from PySide6.QtWidgets import (
        QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
        QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
        QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
        QTabWidget, QVBoxLayout, QWidget,
    )

    from PySide6.QtCore import QThread, Signal

    _active_probes = set()  # keep parentless probes alive until they finish

    class _VmdProbe(QThread):
        """Validate VMD off the GUI thread (keeps the dialog responsive).

        Deliberately parentless: a probe outliving its dialog must not
        crash the app ('QThread destroyed while running'); it finishes,
        its (auto-disconnected) signal goes nowhere, then it deletes
        itself. Probes are kept alive by _active_probes meanwhile.
        """
        done = Signal(bool, str, str, str, str)  # ok, message, ver, path, tachyon

        def __init__(self, path):
            super().__init__(None)
            self.path = path
            _active_probes.add(self)
            self.finished.connect(self._cleanup)

        def _cleanup(self):
            _active_probes.discard(self)
            self.deleteLater()

        def run(self):
            if self.path:
                ok, message, version = validate_vmd(self.path)
            else:
                ok, message, version = False, "not found", ""
            tachyon = find_external_tachyon(self.path) if ok else ""
            self.done.emit(ok, message, version, self.path, tachyon)

    class Dialog(QDialog):
        # ------------------------------------------------------------- init
        def __init__(self, parent, context, version=""):
            super().__init__(parent)
            self.context = context
            self.version = version
            self.closing = False
            self.runner = None
            self.prepared = None
            self.last_result = None
            self.installation = VmdInstallation()

            title = PLUGIN_NAME
            if version:
                title += f"  v{version}"
            self.setWindowTitle(title)
            self.resize(980, 760)

            self.settings = default_settings()
            self._load_persisted()

            # Scrollable content: the results panel appears below the
            # render bar and must stay reachable on any screen size
            from PySide6.QtWidgets import QScrollArea

            scroll = QScrollArea(self)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            content = QWidget()
            root = QVBoxLayout(content)
            root.setContentsMargins(0, 0, 0, 0)
            root.addWidget(self._build_setup_group())
            root.addWidget(self._build_preset_row())
            self.tabs = QTabWidget()
            self.tabs.addTab(self._build_molecule_tab(), "Molecule")
            self.tabs.addTab(self._build_representation_tab(),
                             "Representation")
            self.tabs.addTab(self._build_camera_tab(), "Camera & Scene")
            self.tabs.addTab(self._build_quality_tab(), "Quality")
            self.tabs.addTab(self._build_volumetric_tab(), "Volumetric")
            self.tabs.addTab(self._build_output_tab(), "Output")
            root.addWidget(self.tabs, 1)
            self.results_group = self._build_results_group()
            root.addWidget(self.results_group)
            scroll.setWidget(content)
            outer = QVBoxLayout(self)
            outer.setContentsMargins(4, 4, 4, 4)
            outer.addWidget(scroll, 1)
            # The render bar stays OUTSIDE the scroll area so the
            # Render button is always visible without scrolling
            outer.addWidget(self._build_render_bar())

            self._apply_settings_to_ui()
            self.preset_combo.currentIndexChanged.connect(self._apply_preset)
            self._refresh_molecule_info()
            self._detect_vmd(quiet=True)

        # ------------------------------------------------------------ setup
        def _build_setup_group(self):
            group = QGroupBox("VMD Setup")
            grid = QGridLayout(group)
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(4)

            grid.addWidget(QLabel("VMD executable:"), 0, 0)
            self.vmd_edit = QLineEdit()
            self.vmd_edit.setPlaceholderText(
                "Auto-detected from VMD_BIN, PATH, /opt, ~/…  — or Browse…"
            )
            self.vmd_edit.setToolTip(
                "Path to the 'vmd' launcher of your local installation.\n"
                "Validated by running it in text mode (-dispdev none); no\n"
                "graphics session is opened for the check."
            )
            grid.addWidget(self.vmd_edit, 0, 1)

            browse = QPushButton("Browse…")
            browse.clicked.connect(self._browse_vmd)
            grid.addWidget(browse, 0, 2)

            detect = QPushButton("Detect")
            detect.setToolTip("Re-run automatic detection")
            detect.clicked.connect(lambda: self._detect_vmd(quiet=False))
            grid.addWidget(detect, 0, 3)

            test = QPushButton("Test VMD Installation")
            test.setToolTip(
                "Validate: executable exists and runs, Tcl answers, and the\n"
                "Tachyon engines are present."
            )
            test.clicked.connect(self._test_vmd)
            grid.addWidget(test, 0, 4)

            self.vmd_status = QLabel("not checked")
            self.vmd_status.setWordWrap(True)
            grid.addWidget(self.vmd_status, 1, 0, 1, 5)
            return group

        def _build_preset_row(self):
            row = QHBoxLayout()
            row.addWidget(QLabel("Preset:"))
            self.preset_combo = QComboBox()
            self.preset_combo.setToolTip(
                "Presets set every parameter below; each one stays fully "
                "editable before rendering."
            )
            row.addWidget(self.preset_combo, 1)

            save_preset = QPushButton("Save Preset…")
            save_preset.setToolTip("Store the current settings under a name")
            save_preset.clicked.connect(self._save_custom_preset)
            row.addWidget(save_preset)
            frame = QFrame()
            frame.setLayout(row)
            return frame

        # ------------------------------------------------------------- tabs
        def _build_molecule_tab(self):
            tab = QWidget()
            grid = QGridLayout(tab)
            grid.setVerticalSpacing(6)

            info = QGroupBox("Active Molecule")
            info_grid = QGridLayout(info)
            self.mol_name_label = QLabel("—")
            self.mol_atoms_label = QLabel("—")
            self.mol_3d_label = QLabel("—")
            self.mol_vol_label = QLabel("—")
            for row, (label, value) in enumerate((
                ("Molecule:", self.mol_name_label),
                ("Atoms:", self.mol_atoms_label),
                ("3D structure:", self.mol_3d_label),
                ("Volumetric file:", self.mol_vol_label),
            )):
                info_grid.addWidget(QLabel(label), row, 0)
                info_grid.addWidget(value, row, 1)
                value.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                )
            info_grid.setColumnStretch(1, 1)
            grid.addWidget(info, 0, 0, 1, 2)

            source = QGroupBox("Geometry Source")
            src_grid = QGridLayout(source)
            self.source_current = QCheckBox("Use the current MoleditPy geometry")
            self.source_current.setChecked(True)
            self.source_external = QCheckBox("Load an external geometry (XYZ)")
            self.source_current.toggled.connect(
                lambda on: self.source_external.setChecked(not on)
            )
            self.source_external.toggled.connect(
                lambda on: self.source_current.setChecked(not on)
            )
            src_grid.addWidget(self.source_current, 0, 0, 1, 2)
            src_grid.addWidget(self.source_external, 1, 0, 1, 2)
            self.external_edit = QLineEdit()
            self.external_edit.setPlaceholderText("path/to/geometry.xyz")
            self.external_edit.setEnabled(False)
            self.source_external.toggled.connect(self.external_edit.setEnabled)
            src_grid.addWidget(self.external_edit, 2, 0)
            ext_browse = QPushButton("Browse…")
            ext_browse.setEnabled(False)
            self.source_external.toggled.connect(ext_browse.setEnabled)
            ext_browse.clicked.connect(self._browse_external)
            src_grid.addWidget(ext_browse, 2, 1)
            grid.addWidget(source, 1, 0)

            conn = QGroupBox("Chemistry")
            conn_grid = QGridLayout(conn)
            self.use_connectivity = QCheckBox(
                "Include MoleditPy connectivity (PSF topology)"
            )
            self.use_connectivity.setChecked(True)
            self.use_connectivity.setToolTip(
                "Bonds are exported exactly as in MoleditPy (PSF). VMD never\n"
                "re-derives bonds from distances: radicals and transition\n"
                "states keep their real connectivity."
            )
            conn_grid.addWidget(self.use_connectivity, 0, 0)
            grid.addWidget(conn, 1, 1)
            grid.setRowStretch(2, 1)
            return tab

        def _build_representation_tab(self):
            tab = QWidget()
            grid = QGridLayout(tab)
            grid.setVerticalSpacing(6)

            style = QGroupBox("Style")
            style_grid = QGridLayout(style)
            self.style_combo = QComboBox()
            for key, label in REPRESENTATION_STYLES:
                self.style_combo.addItem(label, key)
            style_grid.addWidget(QLabel("Representation:"), 0, 0)
            style_grid.addWidget(self.style_combo, 0, 1)

            self.show_atoms = QCheckBox("Atoms visible")
            self.show_atoms.setChecked(True)
            self.show_bonds = QCheckBox("Bonds visible")
            self.show_bonds.setChecked(True)
            style_grid.addWidget(self.show_atoms, 1, 0)
            style_grid.addWidget(self.show_bonds, 1, 1)
            grid.addWidget(style, 0, 0, 1, 2)

            sizes = QGroupBox("Geometry")
            size_grid = QGridLayout(sizes)
            self.sphere_scale = QDoubleSpinBox()
            self.sphere_scale.setRange(0.05, 3.0)
            self.sphere_scale.setSingleStep(0.05)
            self.sphere_res = QSpinBox()
            self.sphere_res.setRange(6, 128)
            self.bond_radius = QDoubleSpinBox()
            self.bond_radius.setRange(0.01, 1.0)
            self.bond_radius.setSingleStep(0.01)
            self.bond_res = QSpinBox()
            self.bond_res.setRange(6, 128)
            size_grid.addWidget(QLabel("Sphere scale (× VDW):"), 0, 0)
            size_grid.addWidget(self.sphere_scale, 0, 1)
            size_grid.addWidget(QLabel("Sphere resolution:"), 0, 2)
            size_grid.addWidget(self.sphere_res, 0, 3)
            size_grid.addWidget(QLabel("Bond radius (Å):"), 1, 0)
            size_grid.addWidget(self.bond_radius, 1, 1)
            size_grid.addWidget(QLabel("Bond resolution:"), 1, 2)
            size_grid.addWidget(self.bond_res, 1, 3)
            grid.addWidget(sizes, 1, 0, 1, 2)

            colors = QGroupBox("Colors")
            color_grid = QGridLayout(colors)
            self.color_mode = QComboBox()
            self.color_mode.addItem("Element (per-element)", "element")
            self.color_mode.addItem("Single color", "single")
            color_grid.addWidget(QLabel("Color method:"), 0, 0)
            color_grid.addWidget(self.color_mode, 0, 1)
            self.single_color_btn = QPushButton()
            self.single_color_btn.setFixedWidth(60)
            self.single_color_btn.clicked.connect(self._pick_single_color)
            color_grid.addWidget(self.single_color_btn, 0, 2)
            self.bond_color_combo = QComboBox()
            self.bond_color_combo.addItem("By element (CPK)", "element")
            self.bond_color_combo.addItem("Gray (uniform)", "gray")
            self.bond_color_combo.setToolTip(
                "Enlaces gris uniforme (look de referencia) o coloreados\n"
                "por los elementos que conectan.")
            color_grid.addWidget(QLabel("Bond color:"), 2, 0)
            color_grid.addWidget(self.bond_color_combo, 2, 1, 1, 2)
            grid.addWidget(colors, 2, 0)

            labels = QGroupBox("Atom Labels")
            label_grid = QGridLayout(labels)
            self.labels_combo = QComboBox()
            for key, text in (
                ("none", "None"), ("name", "Name (element + number)"),
                ("index", "Index (0-based, same as MoleditPy)"),
                ("name_index", "Name + index"),
            ):
                self.labels_combo.addItem(text, key)
            self.label_size = QDoubleSpinBox()
            self.label_size.setRange(0.2, 4.0)
            self.label_size.setSingleStep(0.1)
            self.label_color = QComboBox()
            for c in ("black", "white", "yellow", "cyan", "orange", "gray"):
                self.label_color.addItem(c)
            label_grid.addWidget(QLabel("Labels:"), 0, 0)
            label_grid.addWidget(self.labels_combo, 0, 1)
            label_grid.addWidget(QLabel("Size:"), 0, 2)
            label_grid.addWidget(self.label_size, 0, 3)
            label_grid.addWidget(QLabel("Color:"), 0, 4)
            label_grid.addWidget(self.label_color, 0, 5)
            grid.addWidget(labels, 2, 1)
            grid.setRowStretch(3, 1)
            return tab

        def _build_camera_tab(self):
            tab = QWidget()
            grid = QGridLayout(tab)
            grid.setVerticalSpacing(6)

            camera = QGroupBox("Orientation")
            cam_grid = QGridLayout(camera)
            self.orientation = QComboBox()
            for key, label in (
                ("auto_fit", "Auto fit"), ("front", "Front"),
                ("back", "Back"), ("top", "Top"), ("bottom", "Bottom"),
                ("left", "Left"), ("right", "Right"),
                ("isometric", "Isometric"), ("custom", "Custom rotation"),
            ):
                self.orientation.addItem(label, key)
            cam_grid.addWidget(QLabel("View:"), 0, 0)
            cam_grid.addWidget(self.orientation, 0, 1)
            self.rot_x = QDoubleSpinBox(); self.rot_x.setRange(-360, 360)
            self.rot_y = QDoubleSpinBox(); self.rot_y.setRange(-360, 360)
            self.rot_z = QDoubleSpinBox(); self.rot_z.setRange(-360, 360)
            for col, spin in enumerate((self.rot_x, self.rot_y, self.rot_z)):
                spin.setSingleStep(5)
                spin.setSuffix("°")
                spin.setEnabled(False)
            self.orientation.currentIndexChanged.connect(
                lambda: [
                    s.setEnabled(
                        self.orientation.currentData() == "custom"
                    ) for s in (self.rot_x, self.rot_y, self.rot_z)
                ]
            )
            cam_grid.addWidget(QLabel("Custom rotation X/Y/Z:"), 1, 0)
            cam_grid.addWidget(self.rot_x, 1, 1)
            cam_grid.addWidget(self.rot_y, 1, 2)
            cam_grid.addWidget(self.rot_z, 1, 3)
            grid.addWidget(camera, 0, 0, 1, 2)

            match = QGroupBox("MoleditPy Synchronization")
            match_grid = QGridLayout(match)
            self.match_orientation = QCheckBox(
                "Match MoleditPy 3D orientation (camera + zoom)"
            )
            self.match_orientation.setChecked(True)
            self.match_orientation.setToolTip(
                "Reads the PyVista/VTK camera of the 3D view and transfers\n"
                "it exactly to VMD: rotation matrix from the view direction,\n"
                "scale from the FOV / parallel scale (calibrated for VMD 2).\n"
                "Falls back to the selected preset orientation when the 3D\n"
                "view is unavailable."
            )
            self.auto_center = QCheckBox("Auto-center molecule")
            self.auto_center.setChecked(True)
            self.auto_fit_check = QCheckBox("Auto-fit zoom")
            self.auto_fit_check.setChecked(True)
            match_grid.addWidget(self.match_orientation, 0, 0, 1, 2)
            match_grid.addWidget(self.auto_center, 1, 0)
            match_grid.addWidget(self.auto_fit_check, 1, 1)
            grid.addWidget(match, 1, 0)

            proj = QGroupBox("Projection")
            proj_grid = QGridLayout(proj)
            self.projection = QComboBox()
            self.projection.addItem("Perspective", "Perspective")
            self.projection.addItem("Orthographic", "Orthographic")
            proj_grid.addWidget(QLabel("Projection:"), 0, 0)
            proj_grid.addWidget(self.projection, 0, 1)
            grid.addWidget(proj, 1, 1)
            grid.setRowStretch(2, 1)
            return tab

        def _build_quality_tab(self):
            tab = QWidget()
            grid = QGridLayout(tab)
            grid.setVerticalSpacing(6)

            material = QGroupBox("Material & Lighting")
            mat_grid = QGridLayout(material)
            self.material = QComboBox()
            self.material.addItems(VMD_MATERIALS)
            mat_grid.addWidget(QLabel("Material:"), 0, 0)
            mat_grid.addWidget(self.material, 0, 1)
            self.ao_check = QCheckBox("Ambient occlusion")
            self.ao_ambient = QDoubleSpinBox()
            self.ao_ambient.setRange(0.0, 1.0); self.ao_ambient.setSingleStep(0.05)
            self.ao_direct = QDoubleSpinBox()
            self.ao_direct.setRange(0.0, 2.0); self.ao_direct.setSingleStep(0.05)
            mat_grid.addWidget(self.ao_check, 1, 0)
            mat_grid.addWidget(QLabel("AO ambient:"), 1, 1)
            mat_grid.addWidget(self.ao_ambient, 1, 2)
            mat_grid.addWidget(QLabel("AO direct:"), 1, 3)
            mat_grid.addWidget(self.ao_direct, 1, 4)
            self.shadows_check = QCheckBox("Shadows")
            self.shadows_check.setChecked(True)
            self.depthcue_check = QCheckBox("Depth cue (fog)")
            mat_grid.addWidget(self.shadows_check, 2, 0)
            mat_grid.addWidget(self.depthcue_check, 2, 1)
            lights_label = QLabel("Direct lights:")
            mat_grid.addWidget(lights_label, 3, 0)
            self.light_checks = {}
            for idx in range(4):
                check = QCheckBox(str(idx))
                check.setChecked(idx < 2)
                self.light_checks[str(idx)] = check
                mat_grid.addWidget(check, 3, 1 + idx)
            grid.addWidget(material, 0, 0, 1, 2)

            bg = QGroupBox("Background")
            bg_grid = QGridLayout(bg)
            self.background_combo = QComboBox()
            for key, label in (
                ("white", "White"), ("gray", "Gray"),
                ("dark_gray", "Dark Gray"), ("custom", "Custom RGB"),
                ("transparent", "Transparent (post-processed PNG)"),
            ):
                self.background_combo.addItem(label, key)
            self.bg_color_btn = QPushButton()
            self.bg_color_btn.setFixedWidth(60)
            self.bg_color_btn.clicked.connect(self._pick_bg_color)
            bg_grid.addWidget(QLabel("Background:"), 0, 0)
            bg_grid.addWidget(self.background_combo, 0, 1)
            bg_grid.addWidget(self.bg_color_btn, 0, 2)
            self.transparent_note = QLabel(
                "This Tachyon build writes no alpha channel: transparency is "
                "applied in post-processing (render_trans.png)."
            )
            self.transparent_note.setWordWrap(True)
            bg_grid.addWidget(self.transparent_note, 1, 0, 1, 3)
            self.background_combo.currentIndexChanged.connect(
                lambda _: self.transparent_note.setVisible(
                    self.background_combo.currentData() == "transparent"
                )
            )
            grid.addWidget(bg, 1, 0)

            aa = QGroupBox("Anti-aliasing")
            aa_grid = QGridLayout(aa)
            self.aasamples = QComboBox()
            for n in (4, 8, 12, 16, 24, 32):
                self.aasamples.addItem(str(n), n)
            aa_grid.addWidget(QLabel("AA samples/pixel:"), 0, 0)
            aa_grid.addWidget(self.aasamples, 0, 1)
            grid.addWidget(aa, 1, 1)
            grid.setRowStretch(2, 1)
            return tab

        def _build_volumetric_tab(self):
            tab = QWidget()
            grid = QGridLayout(tab)
            grid.setVerticalSpacing(6)

            file_box = QGroupBox("Gaussian Cube File (.cub / .cube / .gz)")
            file_grid = QGridLayout(file_box)
            self.cube_enabled = QCheckBox("Render volumetric data (isosurfaces)")
            self.cube_enabled.toggled.connect(self._on_cube_toggled)
            file_grid.addWidget(self.cube_enabled, 0, 0, 1, 3)
            self.cube_edit = QLineEdit()
            self.cube_edit.setPlaceholderText(
                "path/to/orbital_or_density.cub  (ORCA, Multiwfn, …)"
            )
            self.cube_edit.setEnabled(False)
            file_grid.addWidget(self.cube_edit, 1, 0, 1, 2)
            self.cube_browse = QPushButton("Load Cube…")
            self.cube_browse.setToolTip(
                "Accepts .cub, .cube and gzipped cubes; the file type is\n"
                "verified by content, not by extension. Loading a cube\n"
                "enables the isosurface rendering automatically."
            )
            self.cube_browse.clicked.connect(self._browse_cube)
            file_grid.addWidget(self.cube_browse, 1, 2)
            self.cube_info = QLabel("—")
            self.cube_info.setWordWrap(True)
            file_grid.addWidget(self.cube_info, 2, 0, 1, 3)
            self.cube_interpretation = QComboBox()
            for key, label in (
                ("auto", "Auto-detect from header"),
                ("homo", "HOMO"), ("lumo", "LUMO"),
                ("density", "Electron density"), ("spin_density", "Spin density"),
                ("fukui", "Fukui function"), ("orbital", "Molecular orbital"),
                ("potential", "Potential (MEP/ESP)"), ("other", "Other"),
            ):
                self.cube_interpretation.addItem(label, key)
            file_grid.addWidget(QLabel("Interpretation:"), 3, 0)
            file_grid.addWidget(self.cube_interpretation, 3, 1, 1, 2)
            grid.addWidget(file_box, 0, 0, 1, 2)

            iso = QGroupBox("Isosurfaces (positive vivid blue / negative vivid red)")
            iso_grid = QGridLayout(iso)
            self.iso_pos = QDoubleSpinBox()
            self.iso_pos.setDecimals(6); self.iso_pos.setRange(-99, 99)
            self.iso_pos.setSingleStep(0.001)
            self.iso_neg = QDoubleSpinBox()
            self.iso_neg.setDecimals(6); self.iso_neg.setRange(-99, 99)
            self.iso_neg.setSingleStep(0.001)
            self.pos_color_btn = QPushButton(); self.pos_color_btn.setFixedWidth(60)
            self.neg_color_btn = QPushButton(); self.neg_color_btn.setFixedWidth(60)
            self.pos_color_btn.clicked.connect(
                lambda: self._pick_color_button(self.pos_color_btn))
            self.neg_color_btn.clicked.connect(
                lambda: self._pick_color_button(self.neg_color_btn))
            self.show_pos = QCheckBox("Positive lobe")
            self.show_neg = QCheckBox("Negative lobe")
            self.show_pos.setChecked(True); self.show_neg.setChecked(True)
            iso_grid.addWidget(QLabel("Isovalue +"), 0, 0)
            iso_grid.addWidget(self.iso_pos, 0, 1)
            iso_grid.addWidget(self.pos_color_btn, 0, 2)
            iso_grid.addWidget(self.show_pos, 0, 3)
            iso_grid.addWidget(QLabel("Isovalue −"), 1, 0)
            iso_grid.addWidget(self.iso_neg, 1, 1)
            iso_grid.addWidget(self.neg_color_btn, 1, 2)
            iso_grid.addWidget(self.show_neg, 1, 3)
            self.cube_opacity = QDoubleSpinBox()
            self.cube_opacity.setRange(0.05, 1.0)
            self.cube_opacity.setSingleStep(0.05)
            self.cube_material = QComboBox()
            self.cube_material.addItems(VMD_MATERIALS)
            iso_grid.addWidget(QLabel("Opacity:"), 2, 0)
            iso_grid.addWidget(self.cube_opacity, 2, 1)
            iso_grid.addWidget(QLabel("Material:"), 2, 2)
            iso_grid.addWidget(self.cube_material, 2, 3)
            self.show_molecule_check = QCheckBox(
                "Show molecule together with the surface"
            )
            self.show_molecule_check.setChecked(True)
            iso_grid.addWidget(self.show_molecule_check, 3, 0, 1, 4)
            grid.addWidget(iso, 1, 0, 1, 2)

            map_box = QGroupBox(
                "Property Map (colors the surface by a second cube)"
            )
            map_grid = QGridLayout(map_box)
            self.cube_map_edit = QLineEdit()
            self.cube_map_edit.setPlaceholderText(
                "optional: e.g. userfunc.cub colored on a density.cub surface"
            )
            map_grid.addWidget(self.cube_map_edit, 0, 0, 1, 2)
            self.cube_map_browse = QPushButton("Load Map…")
            self.cube_map_browse.clicked.connect(self._browse_cube_map)
            map_grid.addWidget(self.cube_map_browse, 0, 2)
            self.map_low = QDoubleSpinBox()
            self.map_low.setDecimals(6); self.map_low.setRange(-999, 999)
            self.map_low.setSingleStep(0.005)
            self.map_high = QDoubleSpinBox()
            self.map_high.setDecimals(6); self.map_high.setRange(-999, 999)
            self.map_high.setSingleStep(0.005)
            self.map_colormap = QComboBox()
            for method in ("BWR", "RWB", "RGB", "RBG", "GBR", "GRB",
                           "BRG", "BGR"):
                self.map_colormap.addItem(method)
            map_grid.addWidget(QLabel("Scale min:"), 1, 0)
            map_grid.addWidget(self.map_low, 1, 1)
            map_grid.addWidget(QLabel("Scale max:"), 1, 2)
            map_grid.addWidget(self.map_high, 1, 3)
            map_grid.addWidget(QLabel("Colormap:"), 2, 0)
            map_grid.addWidget(self.map_colormap, 2, 1)
            map_grid.addWidget(QLabel(
                "empty = two-lobe blue/black mode; a map switches to "
                "Volume coloring (BWR) with auto-filled min/max"
            ), 3, 0, 1, 4)
            grid.addWidget(map_box, 2, 0, 1, 2)
            grid.setRowStretch(3, 1)
            return tab

        def _build_output_tab(self):
            tab = QWidget()
            grid = QGridLayout(tab)
            grid.setVerticalSpacing(6)

            folder = QGroupBox("Project Folder")
            folder_grid = QGridLayout(folder)
            self.output_edit = QLineEdit()
            self.output_edit.setPlaceholderText("…/VMD_Renders")
            folder_grid.addWidget(QLabel("Output root:"), 0, 0)
            folder_grid.addWidget(self.output_edit, 0, 1)
            out_browse = QPushButton("Browse…")
            out_browse.clicked.connect(self._browse_output)
            folder_grid.addWidget(out_browse, 0, 2)
            self.project_name_edit = QLineEdit()
            self.project_name_edit.setPlaceholderText("molecule")
            folder_grid.addWidget(QLabel("Project name:"), 1, 0)
            folder_grid.addWidget(self.project_name_edit, 1, 1)
            folder_grid.addWidget(QLabel(
                "each render gets its own molecule_NNN folder (never "
                "overwritten)"), 1, 2)
            grid.addWidget(folder, 0, 0, 1, 2)

            image = QGroupBox("Image")
            image_grid = QGridLayout(image)
            self.res_preset = QComboBox()
            for w in (1024, 1500, 2000, 3000, 4000):
                self.res_preset.addItem(f"{w} × {w}", w)
            self.res_preset.currentIndexChanged.connect(self._apply_res_preset)
            self.width_spin = QSpinBox(); self.width_spin.setRange(64, 16384)
            self.height_spin = QSpinBox(); self.height_spin.setRange(64, 16384)
            self.aspect_check = QCheckBox("Keep viewport aspect")
            self.aspect_check.setToolTip(
                "Set height from the 3D view's aspect ratio when width changes"
            )
            self.aspect_check.toggled.connect(self._on_aspect_toggled)
            image_grid.addWidget(QLabel("Preset:"), 0, 0)
            image_grid.addWidget(self.res_preset, 0, 1)
            image_grid.addWidget(QLabel("Width:"), 0, 2)
            image_grid.addWidget(self.width_spin, 0, 3)
            image_grid.addWidget(QLabel("Height:"), 0, 4)
            image_grid.addWidget(self.height_spin, 0, 5)
            image_grid.addWidget(self.aspect_check, 1, 0, 1, 2)
            self.format_combo = QComboBox()
            self.format_combo.addItem("PNG (lossless from TGA)", "png")
            self.format_combo.addItem("TIFF (needs Pillow)", "tiff")
            self.format_combo.addItem("TGA only (Tachyon native)", "tga")
            image_grid.addWidget(QLabel("Format:"), 1, 2)
            image_grid.addWidget(self.format_combo, 1, 3)
            self.engine_combo = QComboBox()
            self.engine_combo.addItem("Auto", "auto")
            self.engine_combo.addItem("External Tachyon (exact resolution)",
                                      "tachyon")
            self.engine_combo.addItem("TachyonInternal (built-in)", "internal")
            image_grid.addWidget(QLabel("Engine:"), 1, 4)
            image_grid.addWidget(self.engine_combo, 1, 5)
            grid.addWidget(image, 1, 0, 1, 2)
            grid.setRowStretch(2, 1)
            return tab

        # -------------------------------------------------------- render bar
        def _build_render_bar(self):
            box = QGroupBox()
            layout = QHBoxLayout(box)
            self.phase_label = QLabel("Ready.")
            layout.addWidget(self.phase_label, 1)
            self.view_log_btn = QPushButton("View Log…")
            self.view_log_btn.clicked.connect(self._view_log)
            layout.addWidget(self.view_log_btn)
            self.cancel_btn = QPushButton("Cancel")
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.clicked.connect(self._cancel_render)
            layout.addWidget(self.cancel_btn)
            self.render_btn = QPushButton("Render")
            font = QFont(); font.setBold(True)
            self.render_btn.setFont(font)
            self.render_btn.setDefault(True)
            self.render_btn.clicked.connect(self._start_render)
            layout.addWidget(self.render_btn)
            return box

        def _build_results_group(self):
            group = QGroupBox("Result")
            group.setVisible(False)
            layout = QGridLayout(group)

            self.preview_label = QLabel()
            self.preview_label.setMinimumSize(280, 280)
            self.preview_label.setAlignment(
                Qt.AlignmentFlag.AlignCenter
            )
            self.preview_label.setFrameShape(QFrame.Shape.StyledPanel)
            layout.addWidget(self.preview_label, 0, 0, 4, 1)

            self.result_info = QPlainTextEdit()
            self.result_info.setReadOnly(True)
            self.result_info.setMaximumHeight(140)
            layout.addWidget(self.result_info, 0, 1, 1, 3)

            buttons = (
                ("Open Image", self._open_image),
                ("Open Output Folder", self._open_folder),
                ("Open Scene in VMD", self._open_scene),
                ("Re-render", self._rerender),
                ("Re-render edited scene", self._rerender_edited),
            )
            for index, (text, slot) in enumerate(buttons):
                btn = QPushButton(text)
                btn.clicked.connect(slot)
                layout.addWidget(btn, 1 + index // 3, 1 + (index % 3))
            return group

        # ================================================================ UI
        #  <-> settings
        def _collect_settings(self):
            settings = self.settings
            settings["style"] = self.style_combo.currentData()
            settings["sphere_scale"] = self.sphere_scale.value()
            settings["sphere_res"] = self.sphere_res.value()
            settings["bond_radius"] = self.bond_radius.value()
            settings["bond_res"] = self.bond_res.value()
            settings["color_mode"] = self.color_mode.currentData()
            settings["bond_color"] = self.bond_color_combo.currentData()
            settings["single_color"] = self._button_color(
                self.single_color_btn, "#8899aa"
            )
            settings["show_atoms"] = self.show_atoms.isChecked()
            settings["show_bonds"] = self.show_bonds.isChecked()
            settings["labels"] = self.labels_combo.currentData()
            settings["label_size"] = self.label_size.value()
            settings["label_color"] = self.label_color.currentText()
            settings["material"] = self.material.currentText()
            settings["ao"] = self.ao_check.isChecked()
            settings["ao_ambient"] = self.ao_ambient.value()
            settings["ao_direct"] = self.ao_direct.value()
            settings["shadows"] = self.shadows_check.isChecked()
            settings["depthcue"] = self.depthcue_check.isChecked()
            settings["lights"] = {
                idx: check.isChecked()
                for idx, check in self.light_checks.items()
            }
            bg_key = self.background_combo.currentData()
            if bg_key == "transparent":
                settings["background"] = "custom"
                settings["transparent"] = True
                settings["bg_color"] = self._button_color(
                    self.bg_color_btn, "#00ff00"
                )
            else:
                settings["background"] = bg_key
                settings["transparent"] = False
                settings["bg_color"] = self._button_color(
                    self.bg_color_btn, "#ffffff"
                )
            settings["projection"] = self.projection.currentData()
            settings["camera"] = self.orientation.currentData()
            settings["match_moleditpy"] = self.match_orientation.isChecked()
            settings["auto_center"] = self.auto_center.isChecked()
            settings["auto_fit"] = self.auto_fit_check.isChecked()
            settings["custom_rot"] = [
                self.rot_x.value(), self.rot_y.value(), self.rot_z.value()
            ]
            settings["aasamples"] = self.aasamples.currentData()
            settings["geometry_source"] = (
                "current" if self.source_current.isChecked() else "external"
            )
            settings["external_path"] = self.external_edit.text().strip()
            settings["use_connectivity"] = self.use_connectivity.isChecked()
            settings["width"] = self.width_spin.value()
            settings["height"] = self.height_spin.value()
            settings["format"] = self.format_combo.currentData()
            settings["engine"] = self.engine_combo.currentData()
            settings["output_root"] = self.output_edit.text().strip()
            settings["project_name"] = self.project_name_edit.text().strip()
            cube = settings.get("cube") or {}
            cube.update({
                "enabled": self.cube_enabled.isChecked(),
                "path": self.cube_edit.text().strip(),
                "interpretation": self.cube_interpretation.currentData(),
                "iso_pos": self.iso_pos.value(),
                "iso_neg": self.iso_neg.value(),
                "pos_color": self._button_color(self.pos_color_btn, "#1e90ff"),
                "neg_color": self._button_color(self.neg_color_btn, "#e81717"),
                "show_pos": self.show_pos.isChecked(),
                "show_neg": self.show_neg.isChecked(),
                "opacity": self.cube_opacity.value(),
                "material": self.cube_material.currentText(),
                "show_molecule": self.show_molecule_check.isChecked(),
                "map_path": self.cube_map_edit.text().strip(),
                "map_low": self.map_low.value(),
                "map_high": self.map_high.value(),
                "map_colormap": self.map_colormap.currentText(),
            })
            settings["cube"] = cube
            return settings

        def _apply_settings_to_ui(self):
            s = self.settings
            self._select_data(self.style_combo, s.get("style"))
            self.sphere_scale.setValue(float(s.get("sphere_scale", 0.3)))
            self.sphere_res.setValue(int(s.get("sphere_res", 40)))
            self.bond_radius.setValue(float(s.get("bond_radius", 0.15)))
            self.bond_res.setValue(int(s.get("bond_res", 40)))
            self._select_data(self.color_mode, s.get("color_mode"))
            self._select_data(self.bond_color_combo,
                              s.get("bond_color", "element"))
            self._set_button_color(self.single_color_btn, s.get("single_color", "#8899aa"))
            self.show_atoms.setChecked(bool(s.get("show_atoms", True)))
            self.show_bonds.setChecked(bool(s.get("show_bonds", True)))
            self._select_data(self.labels_combo, s.get("labels", "none"))
            self.label_size.setValue(float(s.get("label_size", 1.0)))
            self._select_text(self.label_color, s.get("label_color", "black"))
            self._select_text(self.material, s.get("material", "AOChalky"))
            self.ao_check.setChecked(bool(s.get("ao", True)))
            self.ao_ambient.setValue(float(s.get("ao_ambient", 0.8)))
            self.ao_direct.setValue(float(s.get("ao_direct", 0.4)))
            self.shadows_check.setChecked(bool(s.get("shadows", True)))
            self.depthcue_check.setChecked(bool(s.get("depthcue", False)))
            for idx, check in self.light_checks.items():
                check.setChecked(bool(s.get("lights", {}).get(idx, idx == "0")))
            bg = s.get("background", "white")
            self._select_data(self.background_combo, bg)
            self._set_button_color(self.bg_color_btn, s.get("bg_color", "#ffffff"))
            self._select_data(self.projection, s.get("projection"))
            self._select_data(self.orientation, s.get("camera", "auto_fit"))
            self.match_orientation.setChecked(bool(s.get("match_moleditpy", True)))
            self.auto_center.setChecked(bool(s.get("auto_center", True)))
            self.auto_fit_check.setChecked(bool(s.get("auto_fit", True)))
            rot = s.get("custom_rot", [0, 0, 0])
            self.rot_x.setValue(float(rot[0])); self.rot_y.setValue(float(rot[1]))
            self.rot_z.setValue(float(rot[2]))
            self._select_data(self.aasamples, s.get("aasamples", 12))
            self.source_current.setChecked(
                s.get("geometry_source", "current") == "current"
            )
            self.external_edit.setText(s.get("external_path", ""))
            self.use_connectivity.setChecked(bool(s.get("use_connectivity", True)))
            self.width_spin.setValue(int(s.get("width", 3000)))
            self.height_spin.setValue(int(s.get("height", 3000)))
            self._select_data(self.format_combo, s.get("format", "png"))
            self._select_data(self.engine_combo, s.get("engine", "auto"))
            if not self.output_edit.text():
                self.output_edit.setText(s.get("output_root") or "")
            self.project_name_edit.setText(s.get("project_name", ""))
            cube = s.get("cube") or {}
            self.cube_enabled.setChecked(bool(cube.get("enabled")))
            self.cube_edit.setText(cube.get("path", ""))
            self._select_data(self.cube_interpretation,
                              cube.get("interpretation", "auto"))
            self.iso_pos.setValue(float(cube.get("iso_pos", 0.005)))
            self.iso_neg.setValue(float(cube.get("iso_neg", -0.005)))
            self._set_button_color(self.pos_color_btn, cube.get("pos_color", "#1e90ff"))
            self._set_button_color(self.neg_color_btn, cube.get("neg_color", "#e81717"))
            self.show_pos.setChecked(bool(cube.get("show_pos", True)))
            self.show_neg.setChecked(bool(cube.get("show_neg", True)))
            self.cube_opacity.setValue(float(cube.get("opacity", 0.75)))
            self._select_text(self.cube_material,
                              cube.get("material", "Glossy"))
            self.show_molecule_check.setChecked(bool(cube.get("show_molecule", True)))
            self.cube_map_edit.setText(cube.get("map_path", ""))
            self.map_low.setValue(float(cube.get("map_low", -0.03)))
            self.map_high.setValue(float(cube.get("map_high", 0.0)))
            self._select_text(self.map_colormap,
                              cube.get("map_colormap", "BWR"))
            self._on_cube_toggled()

            self.preset_combo.blockSignals(True)
            self.preset_combo.clear()
            for key, preset in PRESETS.items():
                self.preset_combo.addItem(preset["label"], key)
            for name, preset in self._custom_presets().items():
                self.preset_combo.addItem(f"{name} (custom)", name)
            self.preset_combo.addItem("Custom", "custom")
            self._select_data(self.preset_combo,
                              s.get("preset", "high_quality"))
            self.preset_combo.blockSignals(False)
            # connected once, in __init__: connecting here again after
            # every _apply_settings_to_ui would stack duplicate handlers

        @staticmethod
        def _select_data(combo, data):
            index = combo.findData(data)
            if index >= 0:
                combo.setCurrentIndex(index)

        @staticmethod
        def _select_text(combo, text):
            index = combo.findText(text)
            if index >= 0:
                combo.setCurrentIndex(index)

        def _button_color(self, button, default):
            color = button.property("hex") or default
            return color

        def _set_button_color(self, button, hex_color):
            button.setProperty("hex", hex_color)
            button.setStyleSheet(
                f"background-color: {hex_color}; border: 1px solid #888;"
            )

        def _pick_color_button(self, button):
            current = QColor(button.property("hex") or "#ffffff")
            color = QColorDialog.getColor(current, self)
            if color.isValid():
                self._set_button_color(button, color.name())

        def _pick_single_color(self):
            self._pick_color_button(self.single_color_btn)

        def _pick_bg_color(self):
            self._pick_color_button(self.bg_color_btn)

        # ------------------------------------------------------------ presets
        def _apply_preset(self):
            key = self.preset_combo.currentData()
            if key is None or key == "custom":
                return
            preset = PRESETS.get(key)
            if preset is None:
                preset = self._custom_presets().get(key)
            if preset is None:
                return
            self.settings.update(preset)
            self.settings["preset"] = key
            self._apply_settings_to_ui()

        def _custom_presets(self):
            if self.context is None:
                return {}
            raw = self.context.get_setting(SETTING_CUSTOM_PRESETS, "{}") or "{}"
            try:
                data = json.loads(raw)
                return data if isinstance(data, dict) else {}
            except (ValueError, TypeError):
                return {}

        def _save_custom_preset(self):
            from PySide6.QtWidgets import QInputDialog

            name, ok = QInputDialog.getText(
                self, "Save Preset", "Preset name:"
            )
            if not ok or not name.strip():
                return
            settings = self._collect_settings()
            presets = self._custom_presets()
            presets[name.strip()] = {
                k: v for k, v in settings.items()
                if k not in ("cube", "output_root", "project_name",
                             "external_path", "_natoms")
            }
            if self.context is not None:
                self.context.set_setting(
                    SETTING_CUSTOM_PRESETS,
                    json.dumps(presets, ensure_ascii=False),
                )
            self.preset_combo.blockSignals(True)
            self.preset_combo.clear()
            for key, preset in PRESETS.items():
                self.preset_combo.addItem(preset["label"], key)
            for preset_name in presets:
                self.preset_combo.addItem(f"{preset_name} (custom)", preset_name)
            self.preset_combo.addItem("Custom", "custom")
            self.preset_combo.blockSignals(False)
            self._select_data(self.preset_combo, name.strip())

        # ------------------------------------------------------------ VMD IO
        def _browse_vmd(self):
            path, _ = QFileDialog.getOpenFileName(
                self, "Select the VMD executable",
                self.vmd_edit.text() or "/opt",
            )
            if path:
                self.vmd_edit.setText(path)
                self._save_vmd_path(path)
                self._detect_vmd(quiet=True, forced=path)

        def _detect_vmd(self, quiet=True, forced="", on_validated=None):
            saved = (self.context.get_setting(SETTING_VMD_PATH, "")
                     if self.context else "") or ""
            path = forced or (self.vmd_edit.text().strip() or saved)
            if not path or not _is_executable_file(path):
                path = find_vmd()
            if path:
                self.vmd_edit.setText(path)
            self.vmd_status.setText("checking VMD…")
            self._probe = _VmdProbe(path)
            self._probe_quiet = quiet
            self._probe_callback = on_validated

            def probed(ok, message, version, probe_path, tachyon):
                if self.closing:
                    return
                if ok:
                    self.installation = VmdInstallation(
                        vmd_path=probe_path, tachyon_path=tachyon,
                        version=version,
                    )
                    engine = (
                        "external Tachyon ✓"
                        if tachyon else "built-in TachyonInternal only"
                    )
                    text = f"VMD {version} ready ({engine})."
                    if not tachyon:
                        text += (" Exact resolutions need the external "
                                 "binary.")
                    if self.context is not None:
                        self.context.set_setting(SETTING_VMD_PATH, probe_path)
                else:
                    self.installation = VmdInstallation(vmd_path=probe_path)
                    text = f"VMD not usable: {message}."
                self.vmd_status.setText(text)
                callback = self._probe_callback
                self._probe_callback = None
                if callback is not None:
                    callback(ok, message, version, probe_path, tachyon)
                elif not ok and not self._probe_quiet:
                    QMessageBox.warning(self, "VMD", text)

            self._probe.done.connect(probed)
            self._probe.start()

        def _save_vmd_path(self, path):
            if self.context is not None:
                self.context.set_setting(SETTING_VMD_PATH, path)

        def _test_vmd(self):
            path = self.vmd_edit.text().strip()
            if not path:
                self._detect_vmd(quiet=False)
                return
            self.phase_label.setText("Testing VMD installation…")

            def tested(ok, message, version, probe_path, tachyon):
                lines, missing = diagnose_installation(self.installation)
                if missing:
                    lines.append("")
                    lines.append("Missing dependencies "
                                 f"({len(missing)}):")
                    for name, hint in missing:
                        lines.append(f"  • {hint}")
                else:
                    lines.append("")
                    lines.append("All dependencies present — the render "
                                 "pipeline is fully usable.")
                QMessageBox.information(
                    self, "Test VMD Installation", "\n".join(lines)
                )
                self.phase_label.setText("Ready.")

            self._detect_vmd(quiet=True, forced=path, on_validated=tested)

        # ------------------------------------------------------------- files
        def _browse_external(self):
            path, _ = QFileDialog.getOpenFileName(
                self, "Select an external geometry", "",
                "Geometries (*.xyz *.cub *.cube *.gz);;XYZ Files (*.xyz);;"
                "Cube Files (*.cub *.cube *.gz);;All Files (*)",
            )
            if path:
                self.external_edit.setText(path)
                self._refresh_molecule_info()

        def _browse_cube(self):
            path, _ = QFileDialog.getOpenFileName(
                self, "Select a Gaussian cube file", "",
                "Cube Files (*.cub *.cube *.gaussian.cube *.gz);;"
                "All Files (*)",
            )
            if path:
                self.cube_edit.setText(path)
                # loading a cube is an implicit request to render it
                if not self.cube_enabled.isChecked():
                    self.cube_enabled.setChecked(True)
                self._on_cube_toggled()
                self._update_cube_info()

        def _browse_cube_map(self):
            path, _ = QFileDialog.getOpenFileName(
                self, "Select the property-map cube", "",
                "Cube Files (*.cub *.cube *.gaussian.cube *.gz);;"
                "All Files (*)",
            )
            if path:
                self.cube_map_edit.setText(path)
                if not self.cube_enabled.isChecked():
                    self.cube_enabled.setChecked(True)
                    self._on_cube_toggled()
                    if not self.cube_edit.text().strip():
                        self.cube_info.setText(
                            "Set also the SHAPE cube above (e.g. density.cub);"
                            " this map only colors its surface."
                        )

        def _browse_output(self):
            path = QFileDialog.getExistingDirectory(
                self, "Select the output root", self.output_edit.text() or ""
            )
            if path:
                self.output_edit.setText(path)

        def _on_cube_toggled(self):
            enabled = self.cube_enabled.isChecked()
            for widget in (self.cube_edit, self.iso_pos, self.iso_neg,
                           self.pos_color_btn, self.neg_color_btn,
                           self.show_pos, self.show_neg, self.cube_opacity,
                           self.cube_material, self.show_molecule_check,
                           self.cube_interpretation, self.cube_map_edit):
                widget.setEnabled(enabled)
            # the Browse buttons stay enabled: picking a file enables the
            # whole block by itself
            if enabled and self.cube_edit.text().strip():
                self._update_cube_info()

        def _update_cube_info(self):
            path = self.cube_edit.text().strip()
            header = parse_cube_header(path) if path else None
            if header is None:
                self.cube_info.setText("Not a readable cube file.")
                return
            data_range = cube_data_range(path, header)
            guess = guess_cube_interpretation(header)
            self.cube_info.setText(
                f"{header['natoms']} atoms · grid "
                f"{header['dims'][0]}×{header['dims'][1]}×{header['dims'][2]} "
                f"({header['points']} points) · "
                + (f"data range [{data_range[0]:.3e}, {data_range[1]:.3e}] · "
                   f"guessed: {guess}" if data_range else "data unreadable")
            )
            if data_range:
                suggestion = suggest_isovalue(data_range)
                self.iso_pos.setValue(suggestion)
                self.iso_neg.setValue(-suggestion)
            self._select_data(self.cube_interpretation, guess)

        # -------------------------------------------------- molecule + view
        def _refresh_molecule_info(self):
            mol = self.context.current_mol if self.context else None
            if mol is None or mol.GetNumAtoms() == 0:
                self.mol_name_label.setText("— (no molecule loaded)")
                self.mol_atoms_label.setText("0")
                self.mol_3d_label.setText("missing")
                self.mol_vol_label.setText("—")
                return
            name = (mol.GetProp("_Name") if mol.HasProp("_Name")
                    and mol.GetProp("_Name") else "(untitled)")
            self.mol_name_label.setText(name)
            self.mol_atoms_label.setText(str(mol.GetNumAtoms()))
            self.mol_3d_label.setText(
                "3D ✓" if mol.GetNumConformers() else "no 3D coordinates"
            )
            # A cube opened as the current file is also the volumetric
            # data of this exact geometry: pre-fill the Volumetric tab so
            # the isosurface renders without any extra step.
            current = self.context.get_current_file() if self.context else None
            associated = None
            if current and parse_cube_header(current) is not None:
                associated = current
            elif (self.source_external.isChecked()
                  and parse_cube_header(self.external_edit.text()) is not None):
                associated = self.external_edit.text().strip()
            if associated:
                self.mol_vol_label.setText(
                    f"{Path(associated).name} — isosurface auto-enabled ✓"
                )
                if not self.cube_edit.text().strip():
                    self.cube_edit.setText(associated)
                    self.cube_enabled.setChecked(True)
                    self._on_cube_toggled()
                    self._update_cube_info()
            else:
                self.mol_vol_label.setText(
                    "none associated (load one in the Volumetric tab)"
                )

        def _apply_res_preset(self):
            value = self.res_preset.currentData()
            if value:
                self.width_spin.setValue(int(value))
                self.height_spin.setValue(int(value))

        def _on_aspect_toggled(self, checked):
            if checked and self.context is not None:
                plotter = self.context.plotter
                size = getattr(plotter, "window_size", None) if plotter else None
                if size and size[0] and size[1]:
                    self.height_spin.setValue(
                        int(round(self.width_spin.value() * size[1] / size[0]))
                    )

        # ------------------------------------------------------------ render
        def _start_render(self):
            if self.runner is not None:
                QMessageBox.information(
                    self, PLUGIN_NAME, "A render is already running."
                )
                return
            if not self.installation.vmd_path or not self.installation.version:
                self._detect_vmd(quiet=False)
                if not self.installation.vmd_path:
                    QMessageBox.warning(
                        self, PLUGIN_NAME,
                        "No usable VMD installation found.\n"
                        "Set the executable path above (Browse…/Detect) and\n"
                        "verify it with 'Test VMD Installation'.\n\n"
                        "The plugin never installs or modifies VMD.",
                    )
                    return
            settings = self._collect_settings()
            settings.pop("_natoms", None)
            self._persist_settings()

            log_lines = []

            def log(text):
                log_lines.append(text)

            try:
                prepared = prepare_project(
                    self.context, settings, self.installation, log
                )
            except PreparationError as e:
                QMessageBox.warning(self, PLUGIN_NAME, str(e))
                return
            except (OSError, ValueError, RuntimeError) as e:
                logger.exception("render preparation failed")
                QMessageBox.critical(
                    self, PLUGIN_NAME, f"Render preparation failed:\n{e}"
                )
                return
            for line in log_lines:
                self._append_log_text(line)

            self.prepared = prepared
            self._set_running(True)
            self.results_group.setVisible(False)
            self.runner = RenderRunner(
                parent=self,
                installation=self.installation,
                prepared=prepared,
                on_log=self._append_log_text,
                on_phase=lambda text: self.phase_label.setText(text),
                on_done=self._render_done,
            )
            if self.context is not None:
                self.context.show_status_message(
                    f"{PLUGIN_NAME}: rendering {prepared['molecule_title']}…"
                )
            self.runner.start()

        def _cancel_render(self):
            if self.runner is not None:
                self.runner.cancel()
                self.phase_label.setText("Cancelling…")

        def _render_done(self, result):
            self.runner = None
            self._set_running(False)
            self.last_result = result
            if result["ok"]:
                files = result.get("files") or []
                image = next(
                    (f for f in files if f.endswith(".png")), files[0] if files
                    else ""
                )
                self._show_preview(image)
                info = [
                    f"Molecule: {self.prepared['molecule_title']}",
                    f"Engine: {result['engine']} "
                    f"({self.prepared['engine_reason']})",
                    f"Resolution: {result['resolution'][0]}×"
                    f"{result['resolution'][1]}",
                    f"Time: {result['elapsed']:.1f} s",
                    f"Project: {result['project']}",
                    "Files: " + (", ".join(Path(f).name for f in files)
                                 if files else "output/render.tga"),
                    "Status: OK",
                ]
                self.result_info.setPlainText("\n".join(info))
                self.results_group.setVisible(True)
                self.phase_label.setText(
                    f"Finished in {result['elapsed']:.1f} s → "
                    f"{result['project']}"
                )
                if self.context is not None:
                    self.context.show_status_message(
                        f"{PLUGIN_NAME}: image written to {result['project']}",
                        8000,
                    )
            else:
                self.results_group.setVisible(False)
                self.phase_label.setText(f"FAILED: {result['message']}")
                QMessageBox.critical(
                    self, PLUGIN_NAME,
                    "Rendering failed:\n"
                    f"{result['message']}\n\n"
                    f"Full log: {result['project'] / 'logs' / 'vmd_render.log'}",
                )

        def _set_running(self, running):
            self.render_btn.setEnabled(not running)
            self.cancel_btn.setEnabled(running)
            self.tabs.setEnabled(not running)

        # ------------------------------------------------------------ result
        def _show_preview(self, image_path):
            if not image_path:
                self.preview_label.setText("(no preview)")
                return
            pixmap = QPixmap(image_path)
            if pixmap.isNull() and image_path.lower().endswith(".tga"):
                # Qt cannot read Tachyon's TGA: decode in memory (Pillow,
                # else the plugin's numpy decoder) — nothing written to disk
                try:
                    kind, image = tga_to_image(image_path)
                    if kind == "pil":
                        import numpy as np

                        from PySide6.QtGui import QImage

                        array = np.asarray(image.convert("RGB")).copy()
                        h, w = array.shape[:2]
                        qimage = QImage(
                            array.data, w, h, 3 * w,
                            QImage.Format.Format_RGB888,
                        ).copy()
                        pixmap = QPixmap.fromImage(qimage)
                    else:
                        pixmap = QPixmap.fromImage(image)
                except (ValueError, OSError, RuntimeError):
                    pixmap = QPixmap()
            if pixmap.isNull():
                self.preview_label.setText("(preview unavailable)")
                return
            scaled = pixmap.scaled(
                self.preview_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.preview_label.setPixmap(scaled)

        def _open_image(self):
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl

            files = (self.last_result or {}).get("files") or []
            target = next(
                (f for f in files if f.endswith(".png")),
                str(self._current_project() / "output" / "render.tga"),
            )
            if Path(target).is_file():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

        def _open_folder(self):
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            project = self._current_project()
            if project is not None:
                QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(project / "output"))
                )

        def _open_scene(self):
            """Launch VMD with the scene, exactly as the user would from
            a terminal: correct working directory, scene.vmd (the VMD
            state snapshot that works), no stdio redirection."""
            import subprocess

            project = self._current_project()
            if project is None:
                return
            if not self.installation.vmd_path:
                self._detect_vmd()
                if not self.installation.vmd_path:
                    QMessageBox.warning(self, PLUGIN_NAME,
                                        "No VMD installation found.")
                    return

            scene = project / "scene" / "scene.vmd"
            if not scene.is_file():
                QMessageBox.information(
                    self, PLUGIN_NAME,
                    f"{scene} does not exist yet — render first.",
                )
                return

            # Restore pruned volumetric files before opening
            self._restore_pruned_cubes(project)

            # VMD exits when its stdin reaches EOF (verified: launching
            # from a desktop app inherits /dev/null -> VMD quits right
            # after loading the scene). Fix: run it inside a
            # pseudo-terminal so stdin never EOFs, detached from this
            # process. `script` (util-linux) allocates the pty on Linux;
            # macOS script takes the command as trailing args; the
            # Windows GUI binary does not read stdin this way.
            if sys.platform == "win32":
                subprocess.Popen(
                    [self.installation.vmd_path, "-e", "scene/scene.vmd"],
                    cwd=str(project),
                )
            elif sys.platform == "darwin":
                subprocess.Popen(
                    ["script", "-q", "/dev/null",
                     self.installation.vmd_path, "-e", "scene/scene.vmd"],
                    cwd=str(project), start_new_session=True,
                )
            else:  # Linux
                subprocess.Popen(
                    ["script", "-qec",
                     f'"{self.installation.vmd_path}" '
                     f'-e scene/scene.vmd', "/dev/null"],
                    cwd=str(project), start_new_session=True,
                )

        def _restore_pruned_cubes(self, project):
            """Copy cube files back into volumetric/ if they were pruned
            for the repository (scene.vmd references them)."""
            import json as _json
            import shutil as _shutil

            settings_file = project / "scene" / "render_settings.json"
            if not settings_file.is_file():
                return
            try:
                saved = _json.loads(
                    settings_file.read_text(errors="replace")
                )
                cube_info = saved.get("cube") or {}
                cube_path = cube_info.get("path", "")
                if cube_path and Path(cube_path).is_file():
                    vol_dir = project / "volumetric"
                    vol_dir.mkdir(exist_ok=True)
                    dest = vol_dir / Path(cube_path).name
                    if not dest.is_file():
                        _shutil.copy2(cube_path, dest)
            except (OSError, ValueError):
                pass

        def _rerender(self, edited=False):
            from PySide6.QtCore import QProcess, QProcessEnvironment
            from PySide6.QtWidgets import QProgressDialog

            project = self._current_project()
            if project is None:
                return
            script = (
                "scene/render_edited.tcl" if edited else "scene/render.tcl"
            )
            if not self.installation.vmd_path:
                self._detect_vmd()
                if not self.installation.vmd_path:
                    QMessageBox.warning(self, PLUGIN_NAME, "No VMD found.")
                    return
            width, height = self.width_spin.value(), self.height_spin.value()
            size_w, size_h = vmd_render_size(width, height)
            name = "render_edited" if edited else "render"
            scene_tcl = (project / "scene" / "scene.tcl").read_text(
                errors="replace"
            ) if (project / "scene" / "scene.tcl").is_file() else ""
            resetview = "display resetview" in scene_tcl
            if resetview:
                # reference framing: default VMD window, Tachyon -res
                args = ["-dispdev", "none", "-e", script]
            else:
                args = ["-dispdev", "none", "-size", str(size_w),
                        str(size_h), "-e", script]
            self.phase_label.setText("Re-rendering…")
            progress = QProgressDialog(
                ("Re-rendering " + ("edited scene.vmd" if edited
                                    else "scene.tcl")) + "…",
                "Cancel", 0, 0, self,
            )
            progress.setWindowModality(
                Qt.WindowModality.WindowModal
            )
            process = QProcess(self)
            env = QProcessEnvironment.systemEnvironment()
            process.setProcessEnvironment(env)
            process.setWorkingDirectory(str(project))
            progress.canceled.connect(process.terminate)

            def done(code, _status):
                progress.close()
                engine = (
                    self.prepared["engine"] if self.prepared else
                    engine_choice(self._collect_settings(),
                                  self.installation)[0]
                )
                if code != 0:
                    QMessageBox.warning(
                        self, PLUGIN_NAME, "Re-render failed "
                        f"(exit code {code}). See logs/vmd_render.log."
                    )
                elif engine != "internal":
                    QMessageBox.information(
                        self, PLUGIN_NAME,
                        "Scene rebuilt. Now run Tachyon:\n\n"
                        f"cd {project}\n"
                        f"{self.installation.tachyon_path} tachyon/scene.dat "
                        f"-o output/{name}.tga -res "
                        f"{width} {height}\n\n"
                        "(recorded in the project README)",
                    )
                else:
                    self.phase_label.setText(
                        f"Re-rendered → {project / 'output' / 'render.tga'}"
                    )
                process.deleteLater()

            process.finished.connect(done)
            process.start(self.installation.vmd_path, args)
            process.closeWriteChannel()  # EOF: never wait for Tcl stdin

        def _rerender_edited(self):
            self._rerender(edited=True)

        def _current_project(self):
            if self.prepared is not None:
                return self.prepared["project"]
            return None

        # --------------------------------------------------------------- log
        def _append_log_text(self, text):
            self._log_lines = getattr(self, "_log_lines", [])
            self._log_lines.extend(text.splitlines())
            del self._log_lines[: max(0, len(self._log_lines) - 4000)]

        def _view_log(self):
            project = self._current_project()
            path = (project / "logs" / "vmd_render.log") if project else None
            dlg = QDialog(self)
            dlg.setWindowTitle("VMD render log")
            dlg.resize(760, 480)
            layout = QVBoxLayout(dlg)
            view = QPlainTextEdit(dlg)
            view.setReadOnly(True)
            layout.addWidget(view)
            if path is not None and Path(path).is_file():
                view.setPlainText(Path(path).read_text(errors="replace"))
            else:
                lines = getattr(self, "_log_lines", [])
                view.setPlainText(
                    "\n".join(lines) or "No log yet — render something first."
                )
            dlg.exec()

        # -------------------------------------------------------- persistence
        def _load_persisted(self):
            if self.context is None:
                return
            try:
                state = json.loads(
                    self.context.get_setting(SETTING_GUI_STATE, "{}") or "{}"
                )
            except (ValueError, TypeError):
                state = {}
            if isinstance(state, dict) and state.get("_v") == GUI_STATE_VERSION:
                for key, value in state.items():
                    if key in self.settings:
                        self.settings[key] = value
            elif state:
                # stale state from an older plugin: start from the current
                # defaults (quality presets must reach the user)
                logger.info("discarding pre-v%d GUI state", GUI_STATE_VERSION)
            if not self.settings.get("output_root"):
                self.settings["output_root"] = default_output_root(self.context)

        def _persist_settings(self):
            if self.context is None:
                return
            settings = {
                k: v for k, v in self._collect_settings().items()
                if k not in ("cube", "_natoms")
            }
            settings["cube_path_hint"] = self.cube_edit.text().strip()
            settings["_v"] = GUI_STATE_VERSION
            self.context.set_setting(
                SETTING_GUI_STATE, json.dumps(settings, ensure_ascii=False)
            )
            self.context.set_setting(
                SETTING_OUTPUT_ROOT, self.settings.get("output_root", "")
            )

        # -------------------------------------------------------------- close
        def resizeEvent(self, event):
            super().resizeEvent(event)
            if self.last_result and self.results_group.isVisible():
                files = self.last_result.get("files") or []
                image = next((f for f in files if f.endswith(".png")), "")
                if image:
                    self._show_preview(image)

        def closeEvent(self, event):
            self._persist_settings()
            self.closing = True
            probe = getattr(self, "_probe", None)
            if probe is not None:
                try:
                    probe.done.disconnect()
                except (RuntimeError, TypeError):
                    pass
                self._probe = None
            if self.runner is not None:
                answer = QMessageBox.question(
                    self, "Close " + PLUGIN_NAME,
                    "A render is running. Closing will cancel it. Close?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
                self.runner.cancel()
            self.closing = True
            super().closeEvent(event)

    return Dialog


_DIALOG_CLASS = None


def _lazy_dialog_class():
    global _DIALOG_CLASS, VmdTachyonDialog
    if _DIALOG_CLASS is None:
        _DIALOG_CLASS = _build_dialog_class()
        VmdTachyonDialog = _DIALOG_CLASS
    return _DIALOG_CLASS


def __getattr__(name):
    # Lazy Qt import: VmdTachyonDialog is materialized on first use so that
    # merely loading the plugin stays light and Qt-free.
    if name == "VmdTachyonDialog":
        return _lazy_dialog_class()
    raise AttributeError(name)
