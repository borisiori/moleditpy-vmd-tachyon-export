#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate the README comparison banners for VMD-Tachyon Export.

Three banners, all rendered with the plugin's own pipeline (real VMD +
Tachyon, -trans_raster3d -fullshade, AA 24):

  banner_materials.png        every VMD material, alphabetical
  banner_presets.png          every preset, alphabetical
  banner_representations.png  every representation, alphabetical
                              (on the Publication White preset)

Run with MoleditPy's python from the project root:

    <moleditpy-venv>/bin/python assets/generate_banners.py

The molecule is assets/moleditpy_logo.xyz; renders are thrown away, only
the composed banners are kept (regenerate any time).
"""

import shutil
import subprocess
import sys
import tempfile
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

CELL = 600          # per-render resolution
WORK = Path(tempfile.mkdtemp(prefix="vmd_banners_"))

REPRESENTATION_LABELS = {  # style key -> display name (sorted by label)
    "ball_and_stick": "Ball and Stick",
    "bonds": "Bonds",
    "cpk": "CPK",
    "licorice": "Licorice",
    "lines": "Lines",
    "space_filling": "Space Filling",
}


def logo_molecule():
    mol = Chem.MolFromXYZFile(str(HERE / "moleditpy_logo.xyz"))
    rdDetermineBonds.DetermineBonds(mol, charge=0)
    return mol


class Ctx:
    """Minimal plugin context: the logo molecule, nothing else."""

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


def render_cell(mol, overrides, tag):
    """One pipeline render -> PIL image (or None when VMD is missing)."""
    installation = vmd.detect_installation()
    if not installation.vmd_path:
        raise SystemExit("no VMD installation found — banners need VMD")
    settings = vmd.default_settings()
    settings.update(overrides)
    settings.update({
        "engine": "tachyon", "width": CELL, "height": CELL,
        "output_root": str(WORK / tag),
    })
    settings.pop("_natoms", None)
    prepared = vmd.prepare_project(Ctx(mol), settings, installation,
                                   lambda text: None)
    project = prepared["project"]
    engine = prepared["engine"]
    size = (["-dispdev", "none"] if engine == "tachyon"
            else ["-dispdev", "none", "-size", str(CELL), str(CELL)])
    subprocess.run(
        [installation.vmd_path] + size + ["-e", "scene/build_scene.tcl"],
        cwd=str(project), capture_output=True, timeout=600,
    )
    tga = project / "output" / "render.tga"
    if engine == "tachyon":
        # phase 2 of the pipeline: the standalone renderer with the same
        # quality flags the plugin applies
        if not (project / "tachyon" / "scene.dat").is_file():
            raise RuntimeError(f"{tag}: VMD produced no Tachyon scene")
        subprocess.run(
            [installation.tachyon_path, "tachyon/scene.dat",
             "-o", "output/render.tga", "-res", str(CELL), str(CELL),
             "-aasamples", "24", "-trans_raster3d", "-fullshade"],
            cwd=str(project), capture_output=True, timeout=600,
        )
    if not tga.is_file():
        raise RuntimeError(f"{tag}: render produced no image")
    from PIL import Image

    return Image.open(tga).convert("RGB")


def _font(size, bold=False):
    from PIL import ImageFont

    import matplotlib

    fonts = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(str(fonts / name), size)


def compose_banner(title, cells, columns, out_path, cell_size=340):
    """Grid of labeled renders -> one big banner PNG."""
    from PIL import Image, ImageDraw

    label_font = _font(int(cell_size * 0.062) + 6)
    title_font = _font(int(cell_size * 0.105) + 8, bold=True)
    pad = int(cell_size * 0.05)
    label_h = int(cell_size * 0.14)
    title_h = int(cell_size * 0.24)
    rows = (len(cells) + columns - 1) // columns
    width = columns * (cell_size + pad) + pad
    height = title_h + rows * (cell_size + label_h + pad) + pad
    banner = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(banner)
    draw.text((pad, pad // 2), title, fill=(40, 40, 40), font=title_font)

    for index, (label, image) in enumerate(cells):
        row, col = divmod(index, columns)
        x = pad + col * (cell_size + pad)
        y = title_h + row * (cell_size + label_h + pad)
        thumb = image.resize(
            (cell_size, cell_size), Image.Resampling.LANCZOS
        )
        banner.paste(thumb, (x, y))
        draw.rectangle(
            [x - 1, y - 1, x + cell_size, y + cell_size],
            outline=(210, 210, 210),
        )
        text_w = draw.textlength(label, font=label_font)
        draw.text(
            (x + (cell_size - text_w) // 2, y + cell_size + 6),
            label, fill=(70, 70, 70), font=label_font,
        )
    banner.save(out_path)
    print(f"banner: {out_path}  ({width}x{height})")


def main():
    mol = logo_molecule()
    print(f"logo molecule: {mol.GetNumAtoms()} atoms, "
          f"{mol.GetNumBonds()} bonds")

    # ---- 1. materials (alphabetical, stock forms) ----------------------
    cells = []
    for material in sorted(vmd.VMD_MATERIALS, key=str.lower):
        overrides = {
            "material": material,
            "material_tweaks": {},   # show every material as VMD ships it
            "preset": "custom",
        }
        cells.append((material, render_cell(mol, overrides,
                                            f"mat_{material}")))
        print(f"  material {material}: done")
    compose_banner(
        "VMD materials — one molecule, every material (alphabetical)",
        cells, columns=6, out_path=str(HERE / "banner_materials.png"),
    )

    # ---- 2. presets (alphabetical by label) -----------------------------
    cells = []
    for key, preset in sorted(vmd.PRESETS.items(),
                              key=lambda item: item[1]["label"].lower()):
        overrides = dict(preset)
        overrides["preset"] = key
        cells.append((preset["label"],
                      render_cell(mol, overrides, f"pre_{key}")))
        print(f"  preset {preset['label']}: done")
    compose_banner(
        "Presets — from the default High Quality to publication looks",
        cells, columns=3, out_path=str(HERE / "banner_presets.png"),
        cell_size=420,
    )

    # ---- 3. representations (Publication White, alphabetical) -----------
    cells = []
    base = dict(vmd.PRESETS["publication_white"])
    for style_key, label in sorted(REPRESENTATION_LABELS.items(),
                                   key=lambda item: item[1].lower()):
        overrides = dict(base)
        overrides.update({"style": style_key, "preset": "publication_white"})
        cells.append((label, render_cell(mol, overrides,
                                         f"rep_{style_key}")))
        print(f"  representation {label}: done")
    compose_banner(
        "Representations — Publication White preset, every style",
        cells, columns=3, out_path=str(HERE / "banner_representations.png"),
        cell_size=420,
    )

    shutil.rmtree(WORK, ignore_errors=True)
    print("all banners done")


if __name__ == "__main__":
    main()
