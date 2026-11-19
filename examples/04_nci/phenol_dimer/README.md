# NCI — Phenol Dimer

Non-covalent interaction analysis of a phenol dimer (π-stacking +
hydrogen bond). RDG isosurface at 0.5 colored by sign(λ₂)ρ (BGR
scale ±0.05): blue = attractive (H-bond), green = van der Waals,
red = steric.

![](render.png)

## Files

- `rdg.cub` — reduced density gradient (isosurface shape)
- `signlam2rho.cub` — sign(λ₂)·ρ (coloring)
- `render.png` — the result

## Reproduce

1. Analysis → **NCI / RDG**
2. Volumetric → **Load Cube…** → `rdg.cub`
3. Volumetric → **Load Map…** → `signlam2rho.cub`
4. Render
