"""End-to-end render against the real VMD + Tachyon installation.

Covers: project preparation, exact connectivity export, both engines,
re-render from the editable scene, re-render from an edited snapshot,
cube isosurfaces and transparent-background post-processing. Skips
itself when VMD is not installed.

Run:  python tests/e2e_real_vmd.py   (real work, ~10-30 s)
"""

import importlib.util
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PLUGIN_PATH = Path(__file__).resolve().parent.parent / "VMD-tachyon_export.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "VMD_tachyon_export_e2e", PLUGIN_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeContext:
    """Minimal PluginContext stand-in: current molecule, no 3D view."""

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


def run(cmd, cwd, timeout=300):
    result = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )
    errors = [
        line for line in (result.stdout + result.stderr).splitlines()
        if "ERROR" in line
    ]
    return result.returncode, errors


def image_stats(path):
    from PIL import Image
    import numpy as np

    array = np.array(Image.open(path).convert("RGB"))
    colors, counts = np.unique(array.reshape(-1, 3), axis=0,
                               return_counts=True)
    background = colors[counts.argmax()]
    content = (array != background).any(axis=2).sum()
    return array.shape, background, content / array[:, :, 0].size


def main() -> int:
    vmd = load_plugin()
    installation = vmd.detect_installation()
    if not installation.vmd_path:
        print("SKIP: no VMD installation found")
        return 0
    print(f"VMD {installation.version}: {installation.vmd_path}")
    print(f"external Tachyon: {installation.tachyon_path or '(none)'}")

    from rdkit import Chem
    from rdkit.Chem import AllChem

    benzene = Chem.AddHs(Chem.MolFromSmiles("c1ccccc1"))
    AllChem.EmbedMolecule(benzene, randomSeed=42)
    context = FakeContext(benzene)

    failures = []

    def check(label, condition, detail=""):
        status = "ok  " if condition else "FAIL"
        print(f"[{status}] {label}{(' — ' + detail) if detail else ''}")
        if not condition:
            failures.append(label)

    with tempfile.TemporaryDirectory(prefix="vmd_e2e_") as tmp:
        out_root = str(Path(tmp) / "VMD_Renders")

        # -- 1. external engine render (exact resolution) ----------------
        settings = vmd.default_settings()
        settings.update({
            "engine": "tachyon", "width": 1000, "height": 800,
            "format": "png", "output_root": out_root,
        })
        logs = []
        prepared = vmd.prepare_project(context, settings, installation,
                                       logs.append)
        project = prepared["project"]
        check("project layout created",
              all((project / s).is_dir() for s in vmd.PROJECT_SUBDIRS))
        check("manifest written", (project / "manifest.json").is_file())
        check("project README written", (project / "README.md").is_file())

        size_w, size_h = vmd.vmd_render_size(1000, 800)
        code, errors = run([
            installation.vmd_path, "-dispdev", "none", "-size",
            str(size_w), str(size_h), "-e", "scene/build_scene.tcl",
        ], project)
        check("VMD scene build", code == 0 and not errors,
              "; ".join(errors[:2]))
        check("scene snapshot saved",
              (project / "scene" / "scene.vmd").is_file())
        check("Tachyon scene exported",
              (project / "tachyon" / "scene.dat").stat().st_size > 1000)

        code, _ = run([
            installation.tachyon_path, "tachyon/scene.dat",
            "-o", "output/render.tga", "-res", "1000", "800",
            "-aasamples", "8",
        ], project)
        files, _ = vmd.save_converted_images(
            project / "output" / "render.tga", project / "output",
            "render", "png", False, (255, 255, 255),
        )
        shape, background, content = image_stats(
            project / "output" / "render.png"
        )
        check("external render exact resolution",
              shape[:2] == (800, 1000), f"{shape[1]}x{shape[0]}")
        check("render shows content", content > 0.05,
              f"{100 * content:.1f}% non-background")

        # -- 2. chemistry: connectivity preserved 1:1 -------------------
        psf = (project / "geometry" / "molecule.psf").read_text()
        n_bonds = int(psf.split("!NBOND")[0].strip().splitlines()[-1])
        check("PSF keeps exact RDKit connectivity",
              n_bonds == benzene.GetNumBonds(),
              f"{n_bonds} bonds (RDKit: {benzene.GetNumBonds()})")

        # -- 3. re-render honours scene.tcl edits ------------------------
        scene = (project / "scene" / "scene.tcl").read_text()
        assert "color Display Background white" in scene
        (project / "scene" / "scene.tcl").write_text(scene.replace(
            "color Display Background white",
            "color Display Background black",
        ))
        code, errors = run([
            installation.vmd_path, "-dispdev", "none", "-size",
            str(size_w), str(size_h), "-e", "scene/render.tcl",
        ], project)
        run([
            installation.tachyon_path, "tachyon/scene.dat",
            "-o", "output/render.tga", "-res", "1000", "800",
        ], project)
        _, background2, _ = image_stats(project / "output" / "render.tga")
        check("re-render honours edited scene.tcl",
              code == 0 and tuple(background2) == (0, 0, 0),
              f"background now {tuple(background2)}")

        # -- 4. re-render from an edited scene.vmd snapshot --------------
        (project / "scene" / "_sim_edit.tcl").write_text(
            "play scene/scene.vmd\nrotate y by 90\n"
            "save_state scene/scene.vmd\nexit\n"
        )
        code, _ = run([
            installation.vmd_path, "-dispdev", "none",
            "-e", "scene/_sim_edit.tcl",
        ], project)
        code2, errors2 = run([
            installation.vmd_path, "-dispdev", "none", "-size",
            str(size_w), str(size_h), "-e", "scene/render_edited.tcl",
        ], project)
        check("edited snapshot re-renders",
              code == 0 and code2 == 0 and not errors2)

        # -- 5. built-in engine + transparent background -----------------
        settings = vmd.default_settings()
        settings.update({
            "engine": "internal", "width": 800, "height": 800,
            "background": "dark_gray", "bg_color": "#26262b",
            "transparent": True, "format": "png", "output_root": out_root,
        })
        prepared = vmd.prepare_project(context, settings, installation,
                                       logs.append)
        project = prepared["project"]
        code, errors = run([
            installation.vmd_path, "-dispdev", "none", "-size", "800",
            "800", "-e", "scene/build_scene.tcl",
        ], project)
        check("internal engine render", code == 0 and not errors,
              "; ".join(errors[:2]))
        files, notes = vmd.save_converted_images(
            project / "output" / "render.tga", project / "output",
            "render", "png", True, vmd.resolve_background_rgb(settings),
        )
        from PIL import Image
        import numpy as np

        trans = np.array(Image.open(project / "output" / "render_trans.png"))
        keyed = (trans[:, :, 3] == 0).mean()
        check("transparent background keyed out", keyed > 0.5,
              f"{100 * keyed:.1f}% fully transparent pixels")

        # -- 6. cube isosurfaces (Multiwfn file, 30 atoms -> own molecule)
        cube = Path(__file__).resolve().parent.parent / "test.cub"
        settings = vmd.default_settings()
        settings.update({
            "engine": "internal", "width": 700, "height": 700,
            "output_root": out_root,
        })
        settings["cube"].update({
            "enabled": True, "path": str(cube),
            "iso_pos": 0.004, "iso_neg": -0.004,
        })
        prepared = vmd.prepare_project(context, settings, installation,
                                       logs.append)
        project = prepared["project"]
        check("cube copied into project",
              (project / "volumetric" / "test.cub").is_file())
        code, errors = run([
            installation.vmd_path, "-dispdev", "none", "-size", "704",
            "704", "-e", "scene/build_scene.tcl",
        ], project, timeout=600)
        shape, background, content = image_stats(
            project / "output" / "render.tga"
        )
        check("cube isosurface render", code == 0 and content > 0.05,
              f"{100 * content:.1f}% non-background")

        manifest = Path(project / "manifest.json")
        if manifest.is_file():
            data = __import__("json").loads(manifest.read_text())
            check("cube provenance in manifest",
                  data.get("cube", {}).get("natoms") == 30)

    print()
    if failures:
        print(f"E2E FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("E2E OK: all checks passed against the real VMD installation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
