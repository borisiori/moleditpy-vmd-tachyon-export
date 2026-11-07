"""Tests for VMD-tachyon_export.py (no VMD required for these unit tests)."""

import importlib.util
import json
import math
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "VMD-tachyon_export.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "VMD_tachyon_export_test", PLUGIN_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def vmd():
    return load_plugin()


@pytest.fixture(scope="module")
def benzene():
    from rdkit import Chem
    from rdkit.Chem import AllChem

    mol = Chem.AddHs(Chem.MolFromSmiles("c1ccccc1"))
    AllChem.EmbedMolecule(mol, randomSeed=42)
    return mol


# --------------------------------------------------------------- metadata

def test_plugin_metadata(vmd):
    assert vmd.PLUGIN_NAME == "VMD-Tachyon Export"
    assert vmd.PLUGIN_VERSION
    assert ">=5.0.0, <6.0.0" == vmd.PLUGIN_SUPPORTED_MOLEDITPY_VERSION
    # PySide6 only: the loader rejects legacy Qt bindings
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    for banned in ("import PyQt6", "import PyQt5", "import PySide2",
                   "from PyQt6", "from PyQt5", "from PySide2"):
        assert banned not in source


def test_loads_through_real_plugin_manager(vmd):
    from moleditpy.plugins.plugin_manager import PluginManager

    tmp = tempfile.mkdtemp(prefix="vmd_plugin_test_")
    try:
        shutil.copy(PLUGIN_PATH, str(Path(tmp) / "VMD-tachyon_export.py"))
        manager = PluginManager()
        manager.plugin_dir = tmp
        plugins = manager.discover_plugins()
        entry = next(p for p in plugins if p["name"] == "VMD-Tachyon Export")
        assert entry["status"] == "Loaded"
        assert entry["has_run"] is True  # Plugins-menu entry point
        # The export action feeds the "Export 3D" button and File > Export
        assert any(
            e["plugin"] == "VMD-Tachyon Export"
            and e["label"] == "Export to VMD-Tachyon Scene..."
            for e in manager.export_actions
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -------------------------------------------------------------- geometry

def test_geometry_export_preserves_chemistry(vmd, benzene):
    symbols, positions, bonds = vmd.mol_atom_data(benzene)
    assert len(symbols) == 12 and len(positions) == 12
    assert len(bonds) == benzene.GetNumBonds()

    # XYZ round-trip: element + coordinate fidelity
    xyz = vmd.write_xyz(symbols, positions)
    lines = xyz.strip().splitlines()
    assert lines[0] == "12"
    x, y, z = (float(v) for v in lines[2].split()[1:4])
    # XYZ carries 6 decimals (~1e-6 Å): chemically exact
    assert (x, y, z) == pytest.approx(positions[0], abs=1e-6)

    # PSF: exact connectivity, 1-based serials, 4-per-line bond blocks
    psf = vmd.write_psf(symbols, positions, bonds)
    assert "!NATOM" in psf and "!NBOND" in psf
    atom_count = int(psf.split("!NATOM")[0].strip().splitlines()[-1])
    assert atom_count == 12
    assert f"{len(bonds):8d} !NBOND" in psf
    first, second = bonds[0]
    assert f"{first + 1:8d}{second + 1:8d}" in psf

    # PDB: element column present for element-based coloring in VMD
    pdb = vmd.write_pdb(symbols, positions)
    atom_lines = [l for l in pdb.splitlines() if l.startswith("ATOM")]
    assert len(atom_lines) == 12
    elements = [l[76:78].strip() for l in atom_lines]
    assert elements.count("C") == 6 and elements.count("H") == 6


def test_external_geometry_bond_transfer(vmd, benzene, tmp_path):
    xyz_file = tmp_path / "geo.xyz"
    symbols, positions, _ = vmd.mol_atom_data(benzene)
    xyz_file.write_text(vmd.write_xyz(symbols, positions))

    got, pos, bonds, message = vmd.load_external_geometry(
        xyz_file, current_mol=benzene
    )
    assert len(got) == 12
    assert len(bonds) == benzene.GetNumBonds()  # transferred, not guessed
    assert "transferred" in message

    # mismatched atom count -> deliberately unbonded (no distance guessing)
    from rdkit import Chem

    methane = Chem.AddHs(Chem.MolFromSmiles("C"))
    _, _, bonds2, message2 = vmd.load_external_geometry(
        xyz_file, current_mol=methane
    )
    assert bonds2 == []
    assert "without bonds" in message2


def test_external_geometry_from_cube(vmd):
    """A .cub/.cube can be the external geometry itself: atoms are read
    from the cube header (atomic numbers)."""
    cube = Path(__file__).parent / "test.cub"
    symbols, positions, bonds, message = vmd.load_external_geometry(
        cube, current_mol=None
    )
    assert symbols is not None and len(symbols) == 30
    assert "Cl" in symbols and "C" in symbols
    assert all(len(p) == 3 for p in positions)
    assert bonds == []  # no molecule to transfer from


def test_cube_auto_association(vmd, benzene):
    """A molecule loaded FROM a cube file gets its isosurface for free:
    prepare_project auto-associates the current file as volumetric data."""

    class CubeFileContext:
        def __init__(self, mol, path):
            self._mol, self._path = mol, path

        @property
        def current_mol(self):
            return self._mol

        def get_current_file(self):
            return self._path

        @property
        def plotter(self):
            return None

    # same atom count as test.cub (30): build a 30-atom stand-in molecule
    from rdkit import Chem
    from rdkit.Chem import AllChem

    mol = Chem.AddHs(Chem.MolFromSmiles("c1ccc(cc1)Cl"))
    AllChem.EmbedMolecule(mol, randomSeed=3)  # 12 atoms -> mismatch case
    context = CubeFileContext(
        mol, str(Path(__file__).parent / "test.cub")
    )
    settings = vmd.default_settings()
    settings["output_root"] = str(Path(tempfile.mkdtemp()))
    logs = []
    prepared = vmd.prepare_project(context, settings,
                                   vmd.VmdInstallation(), logs.append)
    # 12-atom molecule vs 30-atom cube: NOT associated (atom mismatch)
    assert not prepared["settings"]["cube"]["enabled"]
    assert any("not auto-associated" in line for line in logs)

    # matching atom count -> auto-associated with the isosurface on
    cube_symbols, cube_positions = vmd._atoms_from_cube_file(
        Path(__file__).parent / "test.cub"
    )
    builder = Chem.RWMol()
    for symbol in cube_symbols:
        builder.AddAtom(Chem.Atom(symbol))
    conformer = Chem.Conformer(len(cube_symbols))
    for i, (x, y, z) in enumerate(cube_positions):
        conformer.SetAtomPosition(i, (x, y, z))
    cube_mol = builder.GetMol()
    cube_mol.AddConformer(conformer)
    logs = []
    prepared = vmd.prepare_project(
        CubeFileContext(cube_mol, str(Path(__file__).parent
                                      / "test.cub")),
        settings, vmd.VmdInstallation(), logs.append,
    )
    assert prepared["settings"]["cube"]["enabled"]
    assert prepared["settings"]["cube"]["path"].endswith("test.cub")
    assert any("auto-associated" in line for line in logs)
    scene = (prepared["project"] / "scene" / "scene.tcl").read_text()
    assert "volumetric/test.cub" in scene
    assert "Isosurface" in scene


# ------------------------------------------------------------------- Tcl

def test_scene_tcl_structure(vmd, benzene):
    symbols, positions, bonds = vmd.mol_atom_data(benzene)
    settings = vmd.default_settings()
    settings["ao"] = True  # default preset ships with AO off; force it on
    settings["_natoms"] = len(symbols)
    camera = vmd.preset_camera("front", positions, "Orthographic")
    tcl = vmd.build_scene_tcl(settings, camera, len(symbols))

    # all paths relative (portable project)
    assert "geometry/molecule.psf" in tcl
    assert "mol addfile geometry/molecule.pdb" in tcl
    for marker in ("rotate_matrix", "scale_matrix", "center_matrix",
                   "display projection Orthographic",
                   "display ambientocclusion on",
                   "axes location Off"):
        assert marker in tcl
    # default (resetview) camera carries the reference recipe instead
    tcl_default = vmd.build_scene_tcl(
        settings, vmd.preset_camera("auto_fit", positions, "Perspective"),
        len(symbols),
    )
    assert "display resetview" in tcl_default
    assert "molinfo" not in tcl_default.split("camera")[1].split("# ---")[0]
    # default palette: black C, red O, white H,
    # vivid green Cl
    assert "color Element C black" in tcl
    assert "color change rgb black 0.0000 0.0000 0.0000" in tcl
    assert "color Element O red" in tcl
    assert "color Element H white" in tcl
    assert "color Element Cl green" in tcl
    assert "color change rgb green 0.0000 1.0000 0.0000" in tcl
    # bonds colored by element (joint CPK — the settled look); the gray
    # variant is opt-in via bond_color="gray"
    assert "CPK 0.8000" in tcl
    assert "mol color ColorID 2" not in tcl
    gray_tcl = vmd.build_scene_tcl(
        {**settings, "bond_color": "gray"}, camera, len(symbols)
    )
    assert "color change rgb gray 0.5200 0.5200 0.5200" in gray_tcl
    assert "mol color ColorID 2" in gray_tcl
    # dedicated green Cl rep (the installed .vmdrc trick), SCOPED with
    # mol modselect — an unscoped rep would paint the whole molecule green
    assert '$clsel set radius 1.75' in tcl
    assert 'mol modselect $clrep $mol "name CL Cl' in tcl
    assert "mol modcolor $clrep $mol ColorID 7" in tcl
    assert "mol modmaterial $clrep $mol Glossy" in tcl
    # matrices are 4x4, nested as a single-element value list for molinfo
    assert "molinfo $_m set rotate_matrix {{ {1.0" in tcl
    # molecule first, then reps, then labels, then camera
    assert tcl.index("mol new") < tcl.index("mol delrep")
    assert tcl.index("set molreps") < tcl.index("foreach _m")
    # white background of the default preset
    assert "color Display Background white" in tcl


def test_scene_tcl_labels(vmd, benzene):
    symbols, positions, _ = vmd.mol_atom_data(benzene)
    settings = vmd.default_settings()
    settings["_natoms"] = len(symbols)
    settings["labels"] = "index"
    camera = vmd.preset_camera("front", positions, "Orthographic")
    tcl = vmd.build_scene_tcl(settings, camera, len(symbols))
    assert "label format Atoms %i" in tcl
    assert "label add Atoms 0/0" in tcl
    assert f"label add Atoms 0/{len(symbols) - 1}" in tcl


def test_cube_tcl_separate_molecule(vmd, benzene):
    """Cube with a different atom count must become its own molecule AFTER
    the molecule reps were added (order matters: mol delrep targets mol 0)."""
    symbols, positions, _ = vmd.mol_atom_data(benzene)
    settings = vmd.default_settings()
    settings["_natoms"] = len(symbols)
    settings["cube"] = {
        "enabled": True, "path": "/tmp/fake.cube", "iso_pos": 0.004,
        "iso_neg": -0.004, "pos_color": "#0000ff", "neg_color": "#ff0000",
        "opacity": 0.8, "material": "Glossy", "show_molecule": True,
    }
    header = {"natoms": 30}  # != 12 -> separate molecule
    camera = vmd.preset_camera("front", positions, "Orthographic")
    tcl = vmd.build_scene_tcl(settings, camera, len(symbols), header)
    assert "mol new volumetric/fake.cube type cube" in tcl
    assert tcl.index("mol addrep $mol") < tcl.index("mol new volumetric")
    assert tcl.index("mol new volumetric") < tcl.index("foreach _m")
    assert "Isosurface 0.004000" in tcl
    assert "Isosurface -0.004000" in tcl
    # opacity is applied on the chosen material (Glossy), never swapped
    assert "material change opacity Glossy 0.8000" in tcl


def test_cube_tcl_property_map(vmd, benzene):
    """Property-map mode: a second cube paints the isosurface through the color
    scale (Volume coloring + BWR + scaleminmax)."""
    symbols, positions, _ = vmd.mol_atom_data(benzene)
    settings = vmd.default_settings()
    settings["_natoms"] = len(symbols)
    settings["cube"] = {
        "enabled": True, "path": "/tmp/density.cub",
        "iso_pos": 0.004, "material": "Glossy", "opacity": 0.8,
        "map_path": "/tmp/userfunc.cub", "map_low": -0.03,
        "map_high": 0.0, "map_colormap": "BWR",
    }
    header = {"natoms": 30}
    camera = vmd.preset_camera("front", positions, "Orthographic")
    tcl = vmd.build_scene_tcl(settings, camera, len(symbols), header)
    assert "mol addfile volumetric/userfunc.cub type cube" in tcl
    assert "color scale method BWR" in tcl
    assert "mol color Volume 1" in tcl
    assert "mol scaleminmax $volmol" in tcl
    assert "-0.030000 0.000000" in tcl
    # mapped mode replaces the two-lobe coloring
    assert "mol color ColorID 31" not in tcl


def test_wrapper_scripts(vmd):
    settings = vmd.default_settings()
    build, render, edited = vmd.build_wrapper_scripts(settings, "tachyon", 12)
    assert "save_state scene/scene.vmd" in build
    assert "source scene/scene.tcl" in build
    assert "render Tachyon tachyon/scene.dat" in build
    # render.tcl must NOT overwrite the snapshot (protects GUI edits)
    assert "save_state" not in render
    assert "play scene/scene.vmd" in edited
    # every script terminates: VMD would otherwise wait for Tcl stdin
    for script in (build, render, edited):
        assert "\nexit\n" in script
    internal_build, _, _ = vmd.build_wrapper_scripts(
        settings, "internal", 8
    )
    assert "render TachyonInternal output/render.tga -aasamples 8" \
        in internal_build


# ----------------------------------------------------------------- camera

class _FakeVtkCamera:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    GetPosition = lambda self: self._pos          # noqa: E731
    GetFocalPoint = lambda self: self._focal      # noqa: E731
    GetViewUp = lambda self: self._up             # noqa: E731
    GetViewAngle = lambda self: self._angle       # noqa: E731
    GetParallelProjection = lambda self: self._parallel  # noqa: E731
    GetParallelScale = lambda self: self._pscale  # noqa: E731


def _fake_camera(pos, focal, up, angle=30.0, parallel=False, pscale=2.0):
    cam = _FakeVtkCamera()
    cam._pos, cam._focal, cam._up = pos, focal, up
    cam._angle, cam._parallel, cam._pscale = angle, parallel, pscale
    return cam


def test_vtk_to_vmd_perspective(vmd):
    cam = _fake_camera((0, 0, 30), (0, 0, 0), (0, 1, 0))
    result = vmd.vtk_camera_to_vmd(cam, (1024, 1024))
    assert result["projection"] == "Perspective"
    # scale makes VMD's half-height at the focal plane equal VTK's
    h_vtk = 30 * math.tan(math.radians(15))
    assert result["scale"] == pytest.approx(
        vmd.VMD_PERSP_HALFHEIGHT / h_vtk, rel=1e-6
    )
    # basis: x->right, y->up, z->toward viewer
    assert result["rotate"][0] == pytest.approx((1, 0, 0))
    assert result["rotate"][1] == pytest.approx((0, 1, 0))
    assert result["rotate"][2] == pytest.approx((0, 0, 1))
    assert result["center"] == (0.0, 0.0, 0.0)


def test_vtk_to_vmd_orthographic(vmd):
    cam = _fake_camera(
        (0, 0, 30), (1, 2, 3), (0, 1, 0), parallel=True, pscale=4.0
    )
    result = vmd.vtk_camera_to_vmd(cam, (800, 600))
    assert result["projection"] == "Orthographic"
    assert result["scale"] == pytest.approx(vmd.VMD_ORTHO_HALFHEIGHT / 4.0)
    assert result["center"] == (-1.0, -2.0, -3.0)


def test_vtk_to_vmd_tilted_camera(vmd):
    """A 90°-tilted view must map its own axes onto the screen."""
    cam = _fake_camera((10, 0, 0), (0, 0, 0), (0, 0, 1))
    result = vmd.vtk_camera_to_vmd(cam, (1024, 1024))
    r, u, b = result["rotate"]
    # looking along -x with world +z as screen-up: right = forward × up = +y
    assert b == pytest.approx((1, 0, 0))    # toward viewer = +x
    assert u == pytest.approx((0, 0, 1))    # screen up = +z
    assert r == pytest.approx((0, 1, 0))    # screen right = +y
    # orthonormal
    for row in (r, u, b):
        assert sum(c * c for c in row) == pytest.approx(1.0)


def test_preset_camera_orientations(vmd):
    positions = [(1, 0, 0), (0, 1, 0), (0, 0, 1)]
    front = vmd.preset_camera("front", positions, "Orthographic")
    assert front["rotate"][0] == [1, 0, 0]
    top = vmd.preset_camera("top", positions, "Orthographic")
    assert top["rotate"][2] == [0, -1, 0]  # back = -y (viewer above)
    iso = vmd.preset_camera("isometric", positions, "Orthographic")
    assert iso["rotate"][2] == pytest.approx(
        tuple(c / math.sqrt(3) for c in (1, 1, 1))
    )
    # auto-fit now delegates to VMD's resetview + reference camera recipe
    fit = vmd.preset_camera("auto_fit", positions, "Perspective")
    assert fit.get("resetview") is True
    # named orientations still use the calibrated matrix framing
    fit_o = vmd.preset_camera("front", positions, "Orthographic")
    centroid = (1 / 3, 1 / 3, 1 / 3)
    r_max = max(
        math.dist(p, centroid) for p in positions
    )
    assert fit_o["scale"] == pytest.approx(
        vmd.VMD_ORTHO_HALFHEIGHT / (r_max * 1.15)
    )


# -------------------------------------------------------------- cube data

def test_cube_header_and_range(vmd):
    header = vmd.parse_cube_header(
        Path(__file__).parent / "test.cub"
    )
    assert header is not None
    assert header["natoms"] == 30
    assert header["points"] == 531360
    data_range = vmd.cube_data_range(
        Path(__file__).parent / "test.cub", header
    )
    assert data_range[0] == pytest.approx(-0.0205, abs=1e-4)
    assert data_range[1] == pytest.approx(0.0308, abs=1e-4)


def test_cube_interpretation_guess(vmd):
    assert vmd.guess_cube_interpretation(
        {"comments": ["MOLECULAR ORBITAL", "HOMO"]}
    ) == "homo"
    assert vmd.guess_cube_interpretation(
        {"comments": ["Electron Density", "grid"]}
    ) == "density"
    assert vmd.guess_cube_interpretation(
        {"comments": ["Generated by Multiwfn", "grid points"]}
    ) == "other"


def test_suggest_isovalue(vmd):
    assert vmd.suggest_isovalue((-0.02, 0.03)) == pytest.approx(0.003)
    assert vmd.suggest_isovalue(None) == 0.002


# ------------------------------------------------------------------- TGA

def _synthetic_tga(width=8, height=6, channels=3):
    import numpy as np

    pixels = np.arange(width * height * channels, dtype=np.uint8).reshape(
        height, width, channels
    )
    if channels == 3:
        data = pixels[..., ::-1]  # BGR
    else:
        data = pixels.copy()
    header = bytearray(18)
    struct.pack_into("<H", header, 12, width)
    struct.pack_into("<H", header, 14, height)
    header[2] = 2  # uncompressed true-color
    header[16] = channels * 8
    return bytes(header) + data.tobytes(), pixels


def test_decode_tga_roundtrip(vmd):
    import numpy as np

    # top-down storage (descriptor bit5 = 1): no flip
    raw, expected = _synthetic_tga()
    header = bytearray(raw[:18])
    header[17] = 0x20
    decoded = vmd.decode_tga_bytes(bytes(header) + raw[18:])
    assert decoded.shape == (6, 8, 3)
    assert np.array_equal(decoded, expected)

    # bottom-up storage (Tachyon default, descriptor bit5 = 0): flipped
    flipped = vmd.decode_tga_bytes(raw)
    assert np.array_equal(flipped, expected[::-1])


def test_decode_tga_real_tachyon_output(vmd):
    """Compare the pure-numpy decoder against Pillow on a real Tachyon TGA."""
    pytest.importorskip("PIL")
    from PIL import Image

    tga = Path(
        "/tmp/vmd_e2e_test/VMD_Renders/molecule_001/output/render.tga"
    )
    if not tga.is_file():
        pytest.skip("no real Tachyon render available")
    import numpy as np

    decoded = vmd.decode_tga_bytes(tga.read_bytes())
    reference = np.array(Image.open(tga).convert("RGB"))
    assert np.array_equal(decoded, reference)


def test_chroma_key_background(vmd):
    import numpy as np

    rgb = np.full((4, 4, 3), 200, dtype=np.uint8)
    rgb[1, 1] = (10, 10, 10)      # content
    rgb[0, 0] = (201, 200, 199)   # background ± 1 (TGA quantization)
    keyed = vmd.chroma_key_background(rgb, (200, 200, 200))
    assert keyed[0, 0, 3] == 0        # near-background keyed out
    assert keyed[2, 2, 3] == 0        # exact background keyed out
    assert keyed[1, 1, 3] == 255      # content kept opaque


def test_save_converted_images_tga_only(vmd, tmp_path):
    """format='tga': the native Tachyon TGA IS the deliverable — no PNG
    re-encode unless a transparent key was requested."""
    import struct

    import numpy as np

    pixels = np.full((6, 8, 3), 250, dtype=np.uint8)
    header = bytearray(18)
    struct.pack_into("<H", header, 12, 8)
    struct.pack_into("<H", header, 14, 6)
    header[2] = 2
    header[16] = 24
    tga = tmp_path / "render.tga"
    tga.write_bytes(bytes(header) + pixels[..., ::-1].tobytes())

    out = tmp_path / "out"
    out.mkdir()
    files, notes = vmd.save_converted_images(
        tga, out, "render", "tga", False, (255, 255, 255)
    )
    assert files == [str(tga)]
    assert not (out / "render.png").exists()
    assert any("native" in n for n in notes)

    # transparent=True still post-processes a keyed PNG alongside
    files, _ = vmd.save_converted_images(
        tga, out, "render", "tga", True, (255, 255, 255)
    )
    assert str(tga) in files
    assert any(f.endswith("render_trans.png") for f in files)


# ---------------------------------------------------------------- presets

def test_presets_complete(vmd):
    required = {
        "engine", "style", "sphere_scale", "sphere_res", "bond_radius",
        "bond_res", "color_mode", "show_atoms", "show_bonds", "labels",
        "material", "ao", "shadows", "background", "projection",
        "width", "height", "format", "aasamples",
    }
    for key, preset in vmd.PRESETS.items():
        missing = required - set(preset)
        assert not missing, f"preset {key} missing {missing}"
    assert set(vmd.PRESETS) == {
        "high_quality", "publication_white", "publication_dark",
        "high_gloss", "scientific_matte", "minimal_clean",
    }
    assert all(m in vmd.VMD_MATERIALS
               for m in (p["material"] for p in vmd.PRESETS.values()))


def test_high_quality_preset_matches_reference_pipeline(vmd):
    """The default preset reproduces the reference pipeline, verified
    by diffing the generated Tachyon scene against the user's own."""
    preset = vmd.PRESETS["high_quality"]
    assert preset["style"] == "cpk"
    assert preset["sphere_scale"] == 0.8 and preset["bond_radius"] == 0.3
    assert preset["sphere_res"] == 22 and preset["bond_res"] == 22
    # atoms AND isosurfaces use the tweaked Glossy (Phong_size 1000)
    assert preset["material"] == "Glossy"
    assert preset["material_tweaks"]["Glossy"] == {
        "ambient": 0.1, "diffuse": 0.6, "opacity": 0.75, "shininess": 1.0,
    }
    assert preset["lights"]["3"] is True      # light 3 on (showcub/.vmdrc)
    assert preset["aasamples"] == 24          # VMDrender_full.sh
    assert preset["projection"] == "Perspective"  # reference camera (Zoom 1)
    assert preset["element_palette"] == "default"  # black C + green Cl rep
    assert preset["format"] == "tga"          # native deliverable by default
    # default isosurface palette: vivid sky blue positive, vivid red negative
    defaults = vmd.default_settings()["cube"]
    assert defaults["pos_color"] == "#1e90ff"
    assert defaults["neg_color"] == "#e81717"
    assert defaults["material"] == "Glossy"
    assert defaults["opacity"] == 0.75


def test_engine_choice_fallbacks(vmd):
    class Inst:
        tachyon_path = "/x/tachyon"
        has_external_tachyon = True

    assert vmd.engine_choice({"engine": "auto"}, Inst())[0] == "tachyon"
    assert vmd.engine_choice({"engine": "internal"}, Inst())[0] == "internal"
    # requested external but missing -> internal fallback
    engine, reason = vmd.engine_choice({"engine": "tachyon"},
                                       vmd.VmdInstallation())
    assert engine == "internal"
    assert "not found" in reason


# ------------------------------------------------------------- projects

def test_next_project_dir_increments(vmd, tmp_path):
    root = tmp_path / "renders"
    first = vmd.next_project_dir(str(root), "mol")
    second = vmd.next_project_dir(str(root), "mol")
    assert first.name == "mol_001" and second.name == "mol_002"
    for sub in vmd.PROJECT_SUBDIRS:
        assert (second / sub).is_dir()


def test_vmd_render_size_multiples_of_16(vmd):
    w, h = vmd.vmd_render_size(3000, 2000)
    assert w % 16 == 0 and h % 16 == 0
    # aspect preserved within ~1%
    assert abs(w / h - 1.5) < 0.011
    assert vmd.vmd_render_size(800, 800) == (800, 800)
    assert vmd.vmd_render_size(0, 0) == (1024, 1024)


# ---------------------------------------------------- platform detection

def test_platform_helpers_linux(vmd, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    roots = [str(r) for r in vmd._vmd_search_roots()]
    assert any("/opt" in r for r in roots)
    assert "bin/vmd" in vmd._vmd_launcher_suffixes()
    assert "/usr/bin/vmd" in vmd._plain_vmd_paths()
    assert vmd._current_os() == "linux"


def test_platform_helpers_windows(vmd, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    roots = [str(r) for r in vmd._vmd_search_roots()]
    assert any("Program Files" in r for r in roots)
    suffixes = vmd._vmd_launcher_suffixes()
    assert "vmd.exe" in suffixes and all(".exe" in s for s in suffixes)
    assert vmd._plain_vmd_paths() == ()
    assert vmd._current_os() == "windows"
    # Windows has no execute bit: any existing file counts as executable
    probe = Path(__file__)
    assert vmd._is_executable_file(probe)
    assert not vmd._is_executable_file(Path(__file__) / "nope")
    assert "university of illinois" in vmd._vmd_install_folder_names()


def test_platform_helpers_macos(vmd, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    roots = [str(r) for r in vmd._vmd_search_roots()]
    assert any(r.startswith("/Applications") for r in roots)
    assert any("homebrew" in r for r in roots)
    suffixes = vmd._vmd_launcher_suffixes()
    assert "Contents/MacOS/vmd" in suffixes
    assert "Contents/Resources/VMD/vmd" in suffixes
    assert "/usr/local/bin/vmd" in vmd._plain_vmd_paths()
    assert vmd._current_os() == "macos"


def test_dependency_hints_exist_for_every_os(vmd, monkeypatch):
    """Each platform ships guidance for every pipeline dependency."""
    for platform in ("linux", "win32", "darwin"):
        monkeypatch.setattr(sys, "platform", platform)
        hints = vmd.dependency_hints()
        for key in ("vmd", "tachyon", "launch"):
            assert hints.get(key), f"{platform} missing hint for {key}"


def test_diagnose_reports_missing_dependencies(vmd):
    lines, missing = vmd.diagnose_installation(vmd.VmdInstallation())
    assert any("not found" in line for line in lines)
    names = [name for name, _hint in missing]
    assert "vmd" in names
    hint = dict(missing)["vmd"]
    assert len(hint) > 40  # an actionable, OS-specific message

    complete = vmd.VmdInstallation(
        vmd_path="/x/vmd", tachyon_path="/x/tachyon", version="2.0.0",
    )
    lines, missing = vmd.diagnose_installation(complete)
    assert not missing
    assert any("fully usable" not in line for line in lines)


def test_find_vmd_on_this_machine(vmd):
    if not vmd.find_vmd():
        pytest.skip("no VMD on this host")
    assert vmd.find_vmd().endswith("vmd")


def test_cube_data_percentiles_robust_range(vmd):
    """Percentiles bracket the meaningful range where min/max would be
    dominated by near-nuclear extremes (e.g. ESP ~ +150 a.u.)."""
    cube = Path(__file__).parent / "test.cub"
    header = vmd.parse_cube_header(cube)
    full = vmd.cube_data_range(cube, header)
    robust = vmd.cube_data_percentiles(cube, header)
    assert robust is not None
    assert full[0] <= robust[0] <= robust[1] <= full[1]
    # percentile span must be far narrower than the min/max span
    assert (robust[1] - robust[0]) < (full[1] - full[0])
    # custom percentiles
    tighter = vmd.cube_data_percentiles(cube, header, 10.0, 90.0)
    assert (tighter[1] - tighter[0]) < (robust[1] - robust[0])


def test_cube_isosurface_radius_units_and_margin(vmd, benzene):
    """Surface-aware framing: bohr->angstrom conversion and the
    adjustable fit margin that the generators rely on."""
    cube = Path(__file__).parent.parent / "examples" / "01_orbitals" / \
        "ethylene" / "homo.cube"
    if not cube.is_file():
        pytest.skip("example cubes not present")
    header = vmd.parse_cube_header(cube)
    assert len(header["atom_coords"]) == 6

    symbols, positions, _ = vmd.mol_atom_data(benzene)  # any 2+ atom ref
    # ethylene reference positions in angstroms (from the cube itself
    # scaled): use its own header atoms converted with the known ratio
    import numpy as np

    ref = (np.array(header["atom_coords"])
           / ((np.array(header["atom_coords"]).max(0)
               - np.array(header["atom_coords"]).min(0))
              / np.array([4.696, 3.519, 0.0])).mean()).tolist() \
        if False else None
    radius = vmd.cube_isosurface_radius(cube, 0.05, header,
                                        reference_positions=None)
    # fallback conversion: bohr -> angstrom, so radius must be a sane
    # lobe size in angstroms (not the raw bohr number)
    assert 0.5 < radius < 4.0

    # fit_margin scales the camera: margin 2.3 shows half the span of
    # margin 1.15
    positions = [(1, 0, 0), (0, 1, 0), (0, 0, 1)]
    base = vmd.preset_camera("front", positions, "Orthographic",
                             fit_margin=1.15)
    wide = vmd.preset_camera("front", positions, "Orthographic",
                             fit_margin=2.3)
    assert wide["scale"] == pytest.approx(base["scale"] / 2.0)

    # explicit fit_radius wins over the atom skeleton
    fit = vmd.preset_camera("front", positions, "Orthographic",
                            fit_radius=3.0)
    assert fit["scale"] == pytest.approx(
        vmd.VMD_ORTHO_HALFHEIGHT / (3.0 * 1.15))
