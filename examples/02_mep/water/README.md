# Water — molecular electrostatic potential (MEP)

MEP mapped on the 0.001 a.u. electron-density surface (van der Waals surface): blue = negative around the oxygen, red = positive on the hydrogens. Symmetric BWR scale ±0.04 a.u., viewed perpendicular to the molecular plane to show both poles.

![MEP of water](render_mep.png)

## Folder contents

* `water.inp` / `.out` / `.gbw`
* `density.cube` (orca_plot) + `esp.cube` (Multiwfn)
* `render_mep.png` + editable `VMD_Renders/` project

## Step-by-step reproduction guide

### 1 · ORCA calculation (B3LYP/def2-SVP, geometry optimization)

```bash
orca water.inp > water.out
```

### 2 · Extract the cube
```bash
# density cube (the surface "shape")
printf "1\n2\n4\n80 80 80\n11\n12\n" | orca_plot water.gbw -i
mv water.eldens.cube density.cube
# molden -> ESP with Multiwfn (function 12, medium grid, export cube)
orca_2mkl water -molden
printf "5\n12\n2\n2\n0\nq\n" | Multiwfn water.molden.input
mv totesp.cub esp.cube
```


### 3 · Render with the VMD-Tachyon Export plugin

1. Open MoleditPy and load the geometry or the cube file
   (**File → Open** `density.cube` (enables the isosurface)).
2. **Plugins → VMD-Tachyon Export** (or the *Export 3D* button).
3. In the **Volumetric** tab: *Isovalue +* **0.001**; under **Property Map → Load
     Map…** choose `esp.cube`, set *Scale min/max* to
     **−0.04 / 0.04** and *Colormap* to **BWR**
4. **Render** → image in `VMD_Renders/molecule_00X/output/`.
   Each render also produces an editable scene (`scene/scene.tcl`,
   `scene.vmd`) for further tweaking in VMD.

### Quick alternative

```bash
<moleditpy-venv>/bin/python examples/generate_examples.py
```
