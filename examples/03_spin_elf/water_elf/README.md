# Agua — dominios de localización electrónica (ELF)

Isosuperficie ELF = 0.8 translúcida (opacidad 0.55): se aislan los dos pares solitarios del oxígeno, los enlaces O–H y los hidrógenos como dominios de localización independientes. ELF calculada con Multiwfn (función 9).

![ELF del agua](render_elf.png)

## Contenido de la carpeta

* `water.inp` / `.out` / `.gbw`
* `elf.cube` (Multiwfn)
* `render_elf.png` + proyecto `VMD_Renders/`

## Paso a paso para reproducir la visualización

### 1 · Cálculo ORCA (B3LYP/def2-SVP, optimización)

```bash
orca water.inp > water.out
```

### 2 · Extraer el cube
```bash
orca_2mkl water -molden
printf "5\n9\n2\n2\n0\nq\n" | Multiwfn water.molden.input
mv elf.cub elf.cube      # ELF (función 9), grid medio
```


### 3 · Renderizar con el plugin VMD-Tachyon Export

1. Abre MoleditPy y carga la geometría o el cube
   (**Archivo → Abrir** `elf.cube` (isosuperficie auto-activada)).
2. **Plugins → VMD-Tachyon Export** (o el botón *Export 3D*).
3. En la pestaña **Volumetric**: *Isovalue +* **0.8** y *Opacity* **0.55** para ver la molécula dentro de los dominios
4. **Render** → imagen en `VMD_Renders/molecule_00X/output/`.
   Cada render deja además la escena editable (`scene/scene.tcl`,
   `scene.vmd`) para retocarla en VMD y volver a renderizar.

### Alternativa: un solo comando

```bash
<moleditpy-venv>/bin/python examples/generate_examples.py
```
