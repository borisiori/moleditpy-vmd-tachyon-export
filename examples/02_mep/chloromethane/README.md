# Clorometano — MEP con el bolsillo del cloro

El MEP distingue el bolsillo negativo alrededor del cloro (azul, la repulsión nucleófila) del lado metilo positivo. Escala asimétrica −0.025 / +0.075 a.u. para aprovechar el rango real del potencial.

![MEP del clorometano](render_mep.png)

## Contenido de la carpeta

* `chloromethane.inp` / `.out` / `.gbw`
* `density.cube` + `esp.cube`
* `render_mep.png` + proyecto `VMD_Renders/`

## Paso a paso para reproducir la visualización

### 1 · Cálculo ORCA (B3LYP/def2-SVP, optimización)

```bash
orca chloromethane.inp > chloromethane.out
```

### 2 · Extraer el cube
```bash
# cube de densidad (la "forma" de la superficie)
printf "1\n2\n4\n80 80 80\n11\n12\n" | orca_plot chloromethane.gbw -i
mv chloromethane.eldens.cube density.cube
# molden → ESP con Multiwfn (función 12, grid medio, exportar cube)
orca_2mkl chloromethane -molden
printf "5\n12\n2\n2\n0\nq\n" | Multiwfn chloromethane.molden.input
mv totesp.cub esp.cube
```


### 3 · Renderizar con el plugin VMD-Tachyon Export

1. Abre MoleditPy y carga la geometría o el cube
   (**Archivo → Abrir** `density.cube` (activa la isosuperficie)).
2. **Plugins → VMD-Tachyon Export** (o el botón *Export 3D*).
3. En la pestaña **Volumetric**: *Isovalue +* **0.001**; **Load Map…** `esp.cube`, *min/max* **−0.025 / 0.075**, **BWR**
4. **Render** → imagen en `VMD_Renders/molecule_00X/output/`.
   Cada render deja además la escena editable (`scene/scene.tcl`,
   `scene.vmd`) para retocarla en VMD y volver a renderizar.

### Alternativa: un solo comando

```bash
<moleditpy-venv>/bin/python examples/generate_examples.py
```
