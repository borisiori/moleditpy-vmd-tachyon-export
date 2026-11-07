# Ethylene — HOMO (π) and LUMO (π*)

The textbook frontier-orbital pair: the bonding HOMO π (two lobes sharing the region between the carbons) and the antibonding LUMO π* (an extra node between them). Positive lobe blue, negative red, isovalue ±0.05, viewed from 45° above the molecular plane.

![HOMO pi of ethylene](render_homo.png)

## Folder contents

* `ethylene.inp` / `ethylene.out` / `ethylene.gbw`
* `homo.cube`, `lumo.cube` (orca_plot, 80³ grid)
* `render_homo.png`, `render_lumo.png` + editable
  `VMD_Renders/` scene projects

## Step-by-step reproduction guide

### 1 · ORCA calculation (B3LYP/def2-SVP, geometry optimization)

```bash
orca ethylene.inp > ethylene.out
```

### 2 · Extract the cube
The HOMO of this molecule is orbital `NO = 7` in the ORBITAL
ENERGIES table (ORCA convention); the LUMO is `NO = 8`:

```bash
# HOMO
printf "2\n7\n4\n80 80 80\n11\n12\n" | orca_plot ethylene.gbw -i
mv ethylene.mo7a.cube homo.cube
# LUMO
printf "2\n8\n4\n80 80 80\n11\n12\n" | orca_plot ethylene.gbw -i
mv ethylene.mo8a.cube lumo.cube
```

(menu: 2 = orbital number · 4 = grid 80³ · 11 = generate · 12 = exit)


### 3 · Render with the VMD-Tachyon Export plugin

1. Open MoleditPy and load the geometry or the cube file
   (**File → Open** `homo.cube` — the plugin detects it as volumetric data and
  enables the isosurface automatically).
2. **Plugins → VMD-Tachyon Export** (or the *Export 3D* button).
3. In the **Volumetric** tab: check *Isovalue +* **0.05** / *−* **−0.05** and the
     blue/red colors; repeat with `lumo.cube` for the LUMO
4. **Render** → image in `VMD_Renders/molecule_00X/output/`.
   Each render also produces an editable scene (`scene/scene.tcl`,
   `scene.vmd`) for further tweaking in VMD.

### Quick alternative

```bash
<moleditpy-venv>/bin/python examples/generate_examples.py
```
> **Orbital numbering**: ORCA (and its `orca_plot` tool) number orbitals
> **from 0** — the HOMO of a closed-shell molecule with 2N electrons
> appears as `NO = N-1` in the ORBITAL ENERGIES table of the `.out`
> file. Multiwfn, in contrast, uses 1-based numbering on the molden
> file. With `orca_plot`, use the number exactly as printed in the
> ORCA table.