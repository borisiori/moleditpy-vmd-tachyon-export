# Quickstart — open a cube, get an image

The fastest workflow: **select the analysis type, load the cube,
press Render**.

## Steps

1. Open MoleditPy
2. **Plugins → VMD-Tachyon Export**
3. Pick an **Analysis** from the dropdown (e.g. NCI, Orbital, MEP)
4. In the **Volumetric** tab, click **Load Cube…** and select your file
   (for MEP/NCI, also load the property-map cube under **Load Map…**)
5. Press **Render**

That's it — the plugin auto-detects the cube type, configures
isovalue/colors/ranges and renders. Each render leaves an editable
VMD scene project you can reopen at any time.

## Files

- `inputs/homo.cube` — the ethylene HOMO orbital cube (also in
  `01_orbitals/`)

## Reproduce the render

```bash
cd VMD_Renders/molecule_001
./open_in_vmd.sh    # opens VMD with the scene loaded
```
