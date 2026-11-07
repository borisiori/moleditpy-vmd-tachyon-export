#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build the examples gallery: run the ORCA calculations, extract the cubes
(ORCA plot tool + Multiwfn) and render every image with the plugin's own
pipeline. Anyone can reproduce the gallery with:

    <moleditpy-venv>/bin/python examples/generate_examples.py

Requirements on PATH (or edit the constants below): orca, orca_plot,
orca_2mkl, Multiwfn. Level: B3LYP/def2-SVP geometry optimization —
seconds per molecule.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT))

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "vmd_tachyon_export", PROJECT / "VMD-tachyon_export.py"
)
vmd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vmd)

from rdkit import Chem  # noqa: E402
from rdkit.Chem import rdDetermineBonds  # noqa: E402

def _find_binary(env_var, name):
    """Locate an external binary: env override, PATH, then a
    Programas/<name> layout under the user's home (no hardcoded
    usernames)."""
    override = os.environ.get(env_var, "").strip()
    if override:
        return override
    found = shutil.which(name)
    if found:
        return found
    guess = Path.home() / "Programas" / name.capitalize() / name
    if name == "orca":
        guess = Path.home() / "Programas" / "ORCA" / "orca"
    for candidate in (guess, guess.parent / "bin" / name):
        if candidate.is_file():
            return str(candidate)
    return name  # let the shell report a clear 'not found'


ORCA = _find_binary("ORCA_BIN", "orca")
MULTIWFN = _find_binary("MULTIWFN_BIN", "Multiwfn")
LEVEL = "! B3LYP def2-SVP Opt"          # simple, seconds per molecule
GRID = "80 80 80"                        # orca_plot grid resolution

MWFN_FUNC = {                             # Multiwfn real-space functions
    "density": 1, "esp": 12, "elf": 9,
}


# ----------------------------------------------------------------- helpers
def run(cmd, cwd, stdin=None, timeout=1200, env=None):
    result = subprocess.run(
        cmd, cwd=str(cwd), input=stdin, capture_output=True, text=True,
        timeout=timeout, env=env,
    )
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip().splitlines()[-6:]
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(map(str, cmd))}"
            f"\n" + "\n".join(tail)
        )
    return result.stdout


def orca_run(folder, inp_name, timeout=1800):
    """ORCA logs to stdout: redirect into <name>.out beside the input."""
    out_path = folder / Path(inp_name).with_suffix(".out").name
    with open(out_path, "w", encoding="utf-8") as handle:
        result = subprocess.run(
            [ORCA, inp_name], cwd=str(folder), stdout=handle,
            stderr=subprocess.STDOUT, text=True, timeout=timeout,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"ORCA failed ({result.returncode}) on {inp_name}; see {out_path}"
        )
    return out_path


def write_inp(folder, name, charge, mult, geometry, extra=""):
    uks = " UKS" if mult != 1 else ""
    inp = folder / f"{name}.inp"
    inp.write_text(
        f"{LEVEL}{uks}\n"
        "%pal nprocs 4 end\n"
        f"{extra}"
        f"* xyz {charge} {mult}\n{geometry.strip()}\n*\n",
        encoding="utf-8",
    )
    return inp


def parse_homo(out_path):
    """Highest occupied MO from the ORBITAL ENERGIES table of the .out."""
    text = Path(out_path).read_text(errors="replace")
    section = re.split(r"ORBITAL ENERGIES", text)[-1]
    homo = 0
    for match in re.finditer(
        r"^\s*(\d+)\s+(2\.0000|1\.0000)\s", section, re.M
    ):
        homo = max(homo, int(match.group(1)))
    if not homo:
        raise RuntimeError(f"no occupations found in {out_path}")
    return homo


def orca_plot_cube(gbw, folder, plot_input):
    """Script one orca_plot cube (menu text discovered empirically)."""
    run([str(Path(ORCA).parent / "orca_plot"), str(gbw), "-i"], folder,
        stdin=plot_input, timeout=600)


def cube_from_orca_plot(gbw, folder, kind, mo=None):
    """kind: 'mo' | 'density' | 'spin' -> path of the produced cube."""
    before = {p.name for p in folder.glob("*.cube")}
    if kind == "mo":
        menu = f"2\n{mo}\n4\n{GRID}\n11\n12\n"
    elif kind == "density":
        menu = f"1\n2\n4\n{GRID}\n11\n12\n"
    else:  # spin
        menu = f"1\n3\n4\n{GRID}\n11\n12\n"
    orca_plot_cube(gbw, folder, menu)
    new = [p for p in folder.glob("*.cube") if p.name not in before]
    if not new:
        raise RuntimeError(f"orca_plot produced no cube ({kind}) in {folder}")
    return new[0]


def cube_from_multiwfn(molden, folder, func_key):
    """Run Multiwfn scripted: func -> medium grid -> export cube."""
    before = {p.name for p in folder.glob("*.cub*")}
    env = dict(os.environ, OMP_STACKSIZE="200M")
    run([MULTIWFN, str(molden)], folder,
        stdin=f"5\n{MWFN_FUNC[func_key]}\n2\n2\n0\nq\n",
        timeout=1800, env=env)
    new = [p for p in folder.glob("*.cub*") if p.name not in before]
    if not new:
        raise RuntimeError(
            f"Multiwfn produced no cube ({func_key}) in {folder}"
        )
    return new[0]


def molden_from_gbw(folder, stem):
    run([str(Path(ORCA).parent / "orca_2mkl"), stem, "-molden"],
        folder, timeout=600)
    molden = folder / f"{stem}.molden.input"
    if not molden.is_file():
        raise RuntimeError(f"orca_2mkl wrote no molden in {folder}")
    return molden


def mol_from_xyz(xyz_path, charge=0):
    mol = Chem.MolFromXYZFile(str(xyz_path))
    try:
        rdDetermineBonds.DetermineBonds(mol, charge=charge)
    except ValueError:
        # radicals: plain connectivity (single bonds) is chemically right
        rdDetermineBonds.DetermineConnectivity(mol)
        Chem.SanitizeMol(
            mol, Chem.SanitizeFlags.SANITIZE_ALL
            ^ Chem.SanitizeFlags.SANITIZE_PROPERTIES
        )
    return mol


class Ctx:
    def __init__(self, mol):
        self._mol = mol

    @property
    def current_mol(self):
        return self._mol

    def get_current_file(self):
        return None

    @property
    def plotter(self):
        return None


def render(folder, mol, overrides):
    """One plugin render into <folder>/VMD_Renders; returns the PNG."""
    installation = vmd.detect_installation()
    if not installation.vmd_path:
        raise SystemExit("no VMD installation found")
    settings = vmd.default_settings()
    # deterministic framing: matrix camera, content (including the
    # isosurface extent) fills ~1/1.55 of the frame — never cropped
    settings.update({"width": 1400, "height": 1160, "format": "png",
                     "camera": "custom", "custom_rot": [0.0, 0.0, 0.0],
                     "fit_margin": 1.55})
    settings.update(overrides)
    settings["output_root"] = str(folder / "VMD_Renders")
    settings.pop("_natoms", None)
    prepared = vmd.prepare_project(Ctx(mol), settings, installation,
                                   lambda text: None)
    project = prepared["project"]
    engine = prepared["engine"]
    size = (["-dispdev", "none"] if engine == "tachyon"
            else ["-dispdev", "none", "-size", "1408", "1168"])
    run([installation.vmd_path] + size + ["-e", "scene/build_scene.tcl"],
        project, timeout=1200)
    tga = project / "output" / "render.tga"
    if engine == "tachyon":
        run([installation.tachyon_path, "tachyon/scene.dat",
             "-o", "output/render.tga", "-res", "1400", "1160",
             "-aasamples", "24", "-trans_raster3d", "-fullshade"],
            project, timeout=1800)
    files, _ = vmd.save_converted_images(
        tga, project / "output", "render", "png", False, (255, 255, 255))
    return next(f for f in files if f.endswith(".png")), project


def gallery_copy(png, folder, name):
    dest = folder / f"{name}.png"
    shutil.copy2(png, dest)
    print(f"    image -> {dest.relative_to(PROJECT)}")
    return dest


# ------------------------------------------------------------- the examples
def example_orbital(folder, name, charge, mult, geometry):
    folder.mkdir(parents=True, exist_ok=True)
    kept = []
    inp = write_inp(folder, name, charge, mult, geometry)
    print(f"  ORCA {name} ...")
    orca_run(folder, inp.name)
    stem = folder / name
    homo = parse_homo(stem.with_suffix(".out"))
    mol = mol_from_xyz(folder / f"{name}.xyz", charge)
    images = []
    views = {"homo": [45.0, 0.0, 0.0],   # 45 deg from above, straight-on
             "lumo": [45.0, 0.0, 0.0]}   # same angle for the pi* pair
    for label, mo in (("homo", homo), ("lumo", homo + 1)):
        cube = cube_from_orca_plot(stem.with_suffix(".gbw"), folder,
                                   "mo", mo=mo)
        cube.rename(folder / f"{label}.cube")
        png, project = render(folder, mol, {
            "preset": "custom",
            "camera": "custom", "custom_rot": views[label],
            "fit_margin": 1.55,
            "cube": {"enabled": True,
                     "path": str(folder / f"{label}.cube"),
                     "iso_pos": 0.05, "iso_neg": -0.05},
        })
        images.append(gallery_copy(png, folder, f"render_{label}"))
        kept.append(project)
    return images, kept


def example_mep(folder, name, charge, mult, geometry, esp_span=None,
                 esp_low=None, esp_high=None, view=(55.0, 10.0, 30.0)):
    folder.mkdir(parents=True, exist_ok=True)
    inp = write_inp(folder, name, charge, mult, geometry)
    print(f"  ORCA {name} ...")
    orca_run(folder, inp.name)
    stem = folder / name
    dens = cube_from_orca_plot(stem.with_suffix(".gbw"), folder, "density")
    dens.rename(folder / "density.cube")
    molden = molden_from_gbw(folder, name)
    esp = cube_from_multiwfn(molden, folder, "esp")
    esp.rename(folder / "esp.cube")
    mol = mol_from_xyz(folder / f"{name}.xyz", charge)
    png, _project = render(folder, mol, {
        "preset": "custom",
        "camera": "custom", "custom_rot": list(view),
        "fit_margin": 1.55,
        "cube": {"enabled": True, "path": str(folder / "density.cube"),
                 "iso_pos": 0.001,
                 "map_path": str(folder / "esp.cube"),
                 "map_colormap": "BWR",
                 "map_low": esp_low if esp_low is not None else -esp_span,
                 "map_high": esp_high if esp_high is not None else esp_span,
                 "map_range_set": True},
    })
    return [gallery_copy(png, folder, "render_mep")], [_project]


def example_spin(folder, name, charge, mult, geometry, view=None):
    folder.mkdir(parents=True, exist_ok=True)
    inp = write_inp(folder, name, charge, mult, geometry)
    print(f"  ORCA {name} ...")
    orca_run(folder, inp.name)
    stem = folder / name
    spin = cube_from_orca_plot(stem.with_suffix(".gbw"), folder, "spin")
    spin.rename(folder / "spindens.cube")
    mol = mol_from_xyz(folder / f"{name}.xyz", charge)
    overrides = {
        "preset": "custom",
        "cube": {"enabled": True,
                 "path": str(folder / "spindens.cube"),
                 "iso_pos": 0.002, "iso_neg": -0.002},
    }
    if view:
        overrides["camera"] = "custom"
        overrides["custom_rot"] = list(view)
    png, _project = render(folder, mol, overrides)
    return [gallery_copy(png, folder, "render_spin")], [_project]


def example_elf(folder, name, charge, mult, geometry, view=None):
    folder.mkdir(parents=True, exist_ok=True)
    inp = write_inp(folder, name, charge, mult, geometry)
    print(f"  ORCA {name} ...")
    orca_run(folder, inp.name)
    stem = folder / name
    molden = molden_from_gbw(folder, name)
    elf = cube_from_multiwfn(molden, folder, "elf")
    elf.rename(folder / "elf.cube")
    mol = mol_from_xyz(folder / f"{name}.xyz", charge)
    overrides = {
        "preset": "custom",
        "cube": {"enabled": True, "path": str(folder / "elf.cube"),
                 "iso_pos": 0.8,          # localization domains
                 "opacity": 0.55},        # molecule visible inside
    }
    if view:
        overrides["camera"] = "custom"
        overrides["custom_rot"] = list(view)
    png, _project = render(folder, mol, overrides)
    return [gallery_copy(png, folder, "render_elf")], [_project]


GEOMETRIES = {
    "ethylene": (
        0, 1,
        "C    0.6675    0.0000    0.0000\n"
        "C   -0.6675    0.0000    0.0000\n"
        "H    1.2320    0.9210    0.0000\n"
        "H    1.2320   -0.9210    0.0000\n"
        "H   -1.2320    0.9210    0.0000\n"
        "H   -1.2320   -0.9210    0.0000",
    ),
    "formaldehyde": (
        0, 1,
        "C    0.0000    0.0000    0.0000\n"
        "O    0.0000    0.0000    1.2100\n"
        "H    0.9400    0.0000   -0.5500\n"
        "H   -0.9400    0.0000   -0.5500",
    ),
    "water": (
        0, 1,
        "O    0.0000    0.0000    0.1170\n"
        "H    0.0000    0.7570   -0.4690\n"
        "H    0.0000   -0.7570   -0.4690",
    ),
    "chloromethane": (
        0, 1,
        "C    0.0000    0.0000    0.0000\n"
        "Cl   0.0000    0.0000    1.7800\n"
        "H    1.0300    0.0000   -0.3900\n"
        "H   -0.5150    0.8920   -0.3900\n"
        "H   -0.5150   -0.8920   -0.3900",
    ),
    "methyl_radical": (
        0, 2,
        "C    0.0000    0.0000    0.0000\n"
        "H    0.0000    1.0790    0.0000\n"
        "H    0.9340   -0.5390    0.0000\n"
        "H   -0.9340   -0.5390    0.0000",
    ),
}


def prune_for_repo(keep):
    """Strip regenerable bulk so the gallery stays repository-friendly.

    Removed: render projects from PREVIOUS runs (only the projects of
    the run that just finished are kept), cubes duplicated inside each
    render project (the originals stay at the example top level), the
    15 MB Tachyon scene dumps, the native .tga (a lossless .png copy is
    kept) and the molden files (one orca_2mkl command brings them
    back, see the README).
    """
    keep = set(str(p) for p in keep)
    for folder in HERE.glob("0*_*/*"):
        for project in (folder / "VMD_Renders").glob("molecule_*"):
            if str(project) not in keep:
                shutil.rmtree(project, ignore_errors=True)
                continue
            for cube in (project / "volumetric").glob("*"):
                cube.unlink()
            dat = project / "tachyon" / "scene.dat"
            if dat.is_file():
                dat.unlink()
            for tga in (project / "output").glob("*.tga"):
                tga.unlink()
        for molden in folder.glob("*.molden.input"):
            molden.unlink()
        for scratch in list(folder.glob("*.tmp")) + \
                list(folder.glob("*.densities")) + \
                list(folder.glob("*.densitiesinfo")) + \
                list(folder.glob("*.engrad")) + \
                list(folder.glob("*.property.txt")) + \
                list(folder.glob("*.opt")) + \
                list(folder.glob("*.bibtex")) + \
                list(folder.glob("*.xyz")) + \
                list(folder.glob("*.structures")) + \
                list(folder.glob("MOvalue.cub")) + \
                list(folder.glob("totesp.cub")) + \
                list(folder.glob("80")):
            scratch.unlink()  # kept: inp, out, gbw, cubes, png, projects


def main():
    jobs = [
        ("01_orbitals/ethylene", example_orbital, "ethylene", {}),
        ("01_orbitals/formaldehyde", example_orbital, "formaldehyde", {}),
        ("02_mep/water", example_mep, "water",
         {"esp_low": -0.04, "esp_high": 0.04,
          "view": (0.0, 90.0, 0.0)}),
        ("02_mep/chloromethane", example_mep, "chloromethane",
         {"esp_low": -0.025, "esp_high": 0.075,
          "view": (0.0, 90.0, 0.0)}),
        ("03_spin_elf/methyl_radical", example_spin, "methyl_radical",
         {"view": (90.0, 0.0, 0.0)}),
        ("03_spin_elf/water_elf", example_elf, "water",
         {"view": (0.0, 90.0, 0.0)}),
    ]
    kept_projects = []
    for relative, builder, molecule, extra in jobs:
        folder = HERE / relative
        print(f"[{relative}]")
        # deterministic numbering: every run starts fresh at molecule_001
        shutil.rmtree(folder / "VMD_Renders", ignore_errors=True)
        charge, mult, geometry = GEOMETRIES[molecule]
        _images, kept = builder(folder, molecule, charge, mult,
                                geometry, **extra)
        kept_projects.extend(kept)
    prune_for_repo(kept_projects)
    print("all examples done (pruned for the repository)")


if __name__ == "__main__":
    main()
