# Chloromethane — MEP with the chlorine pocket

The MEP distinguishes the negative pocket around the chlorine (blue, nucleophilic repulsion) from the positive methyl side. Asymmetric scale −0.025 / +0.075 a.u. to exploit the actual range of the potential, viewed perpendicular to the C–Cl axis.

![MEP of chloromethane](render_mep.png)

## Folder contents

* `chloromethane.inp` / `.out` / `.gbw`
* `density.cube` + `esp.cube`
* `render_mep.png` + editable `VMD_Renders/` project

## Step-by-step reproduction guide

### 1 · ORCA calculation (B3LYP/def2-SVP, geometry optimization)

```bash
orca chloromethane.inp > chloromethane.out
```

### 2 · Extract the cube
```bash
# density cube (the surface "shape")
printf "1\n2\n4\n80 80 80\n11\n12\n" | orca_plot chloromethane.gbw -i
mv chloromethane.eldens.cube density.cube
# molden -> ESP with Multiwfn (function 12, medium grid, export cube)
orca_2mkl chloromethane -molden
printf "5\n12\n2\n2\n0\nq\n" | Multiwfn chloromethane.molden.input
mv totesp.cub esp.cube
```


### 3 · Render with the VMD-Tachyon Export plugin

1. Open MoleditPy and load the geometry or the cube file
   (**File → Open** `density.cube` (enables the isosurface)).
2. **Plugins → VMD-Tachyon Export** (or the *Export 3D* button).
3. In the **Volumetric** tab: *Isovalue +* **0.001**; **Load Map…** `esp.cube`,
     *min/max* **−0.025 / 0.075**, **BWR**
4. **Render** → image in `VMD_Renders/molecule_00X/output/`.
   Each render also produces an editable scene (`scene/scene.tcl`,
   `scene.vmd`) for further tweaking in VMD.

### Quick alternative

```bash
<moleditpy-venv>/bin/python examples/generate_examples.py
```
