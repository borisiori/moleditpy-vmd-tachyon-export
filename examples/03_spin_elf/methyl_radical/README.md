# Radical metilo — densidad de espín

Cálculo no restringido (UKS, multiplicidad 2): toda la densidad de espín es α — un solo lobo azul sobre el carbono radicalario, plano como corresponde a un radical de tipo π. Isovalor ±0.002.

![Densidad de espín del radical metilo](render_spin.png)

## Contenido de la carpeta

* `methyl_radical.inp` (`UKS`, mult 2) / `.out` / `.gbw`
* `spindens.cube`
* `render_spin.png` + proyecto `VMD_Renders/`

## Paso a paso para reproducir la visualización

### 1 · Cálculo ORCA (B3LYP/def2-SVP, optimización)

```bash
orca methyl_radical.inp > methyl_radical.out
```

### 2 · Extraer el cube
```bash
printf "1\n3\n4\n80 80 80\n11\n12\n" | orca_plot methyl_radical.gbw -i
mv methyl_radical.spindens.cube spindens.cube
```

(menú: 1 = tipo → 3 = densidad de espín SCF)


### 3 · Renderizar con el plugin VMD-Tachyon Export

1. Abre MoleditPy y carga la geometría o el cube
   (**Archivo → Abrir** `spindens.cube` (isosuperficie auto-activada)).
2. **Plugins → VMD-Tachyon Export** (o el botón *Export 3D*).
3. En la pestaña **Volumetric**: ajusta *Isovalue* a ±0.002; solo el lobo azul (α) aparecerá
4. **Render** → imagen en `VMD_Renders/molecule_00X/output/`.
   Cada render deja además la escena editable (`scene/scene.tcl`,
   `scene.vmd`) para retocarla en VMD y volver a renderizar.

### Alternativa: un solo comando

```bash
<moleditpy-venv>/bin/python examples/generate_examples.py
```
