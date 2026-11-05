# Example 1 — cube cargado como molécula → isosuperficie automática

Este ejemplo reproduce **exactamente el flujo real**: la molécula se cargó
en MoleditPy desde `test.cub` (30 átomos, sistema aromático clorado) y el
plugin detectó que el archivo abierto **es a la vez el dato volumétrico**,
asociando la isosuperficie sin tocar la pestaña Volumetric:

```
[cube] auto-associated from the loaded file: test.cub
       (same system — isosurface enabled)
```

## Contenido

```
example_1/
├── README.md                    este archivo
├── render_preview.png           vista previa del resultado
├── inputs/                      LO QUE SE USÓ
│   ├── test.cub                 cube de Multiwfn (molécula + dato volumétrico)
│   └── molecule_from_testcub.pdb  geometría 3D tal como MoleditPy la percibió
└── VMD_Renders/molecule_001/    LO QUE SE GENERÓ (proyecto completo)
    ├── geometry/                molecule.psf (31 enlaces exactos) + .pdb + .xyz
    ├── volumetric/test.cub      copia del dato volumétrico
    ├── scene/scene.tcl          escena editable (rutas relativas)
    ├── scene/scene.vmd          snapshot VMD
    ├── scene/render.tcl         re-render sin MoleditPy
    ├── scene/render_edited.tcl  re-render tras editar en el GUI de VMD
    ├── tachyon/scene.dat        escena Tachyon (motor externo)
    ├── output/render.tga         imagen final NATIVA Tachyon (1400×1100, AA 24)
    ├── logs/vmd_render.log      transcripción completa
    ├── manifest.json            procedencia completa
    └── README.md                cómo reabrir/editar/re-renderizar
```

## Estilo usado (preset High Quality)

* **carbono negro, oxígeno rojo, hidrógeno blanco**, cloro **verde vivo**
  (rep dedicada, radio vdW 1.75); enlaces por elemento (CPK conjunto)
* CPK 0.8 / 0.3 / resolución 22; cámara Perspective con
  resetview + distance −8 / height 10 (Zoom 1, como tus renders)
* átomos E isosuperficies con **Glossy** (ambient 0.1, diffuse 0.6,
  opacity 0.75, shininess 1.0 → Phong_size 1000, brillos nítidos)
* isosuperficies **azul cielo vivo** (positiva) y **roja viva** (negativa),
  Glossy (ambient 0.1, diffuse 0.6, opacity 0.75, shininess 1.0)
* Tachyon externo con `-trans_raster3d -fullshade`
* luces 0/1/3, proyección ortográfica, fondo blanco

## Re-renderizar sin MoleditPy

```bash
cd example_1/VMD_Renders/molecule_001
/home/borisiori/Programas/VMD/bin/vmd -dispdev none -e scene/render.tcl
/home/borisiori/Programas/VMD/lib/vmd/tachyon_LINUXAMD64 \
    tachyon/scene.dat -o output/render.tga -res 1400 1100 -aasamples 24
```

## Cómo se hizo

1. Abrir `inputs/test.cub` en MoleditPy (File → Open).
2. Plugins → VMD-Tachyon Export → Render (preset por defecto).
3. El plugin detecta que el archivo abierto es un cube del mismo sistema
   y activa la isosuperficie automáticamente.
