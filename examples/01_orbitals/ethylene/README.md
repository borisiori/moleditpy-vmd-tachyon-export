# Etileno — orbitales HOMO (π) y LUMO (π*)

El par textbook de orbitales de frontera: el HOMO π enlazante (dos lóbulos, un nodo en el plano molecular) y el LUMO π* antienlazante (nodo adicional entre los carbonos). Lóbulo positivo azul, negativo rojo, isovalor ±0.05.

![HOMO pi del etileno](render_homo.png)

## Contenido de la carpeta

* `ethylene.inp` / `ethylene.out` / `ethylene.gbw`
* `homo.cube`, `lumo.cube` (orca_plot, grid 80³)
* `render_homo.png`, `render_lumo.png` + proyectos `VMD_Renders/`

## Paso a paso para reproducir la visualización

### 1 · Cálculo ORCA (B3LYP/def2-SVP, optimización)

```bash
orca ethylene.inp > ethylene.out
```

### 2 · Extraer el cube
El HOMO de esta molécula es el orbital `NO = 7` de la tabla ORBITAL
ENERGIES (convención ORCA); el LUMO es `NO = 8`:

```bash
# HOMO
printf "2\n7\n4\n80 80 80\n11\n12\n" | orca_plot ethylene.gbw -i
mv ethylene.mo7a.cube homo.cube
# LUMO
printf "2\n8\n4\n80 80 80\n11\n12\n" | orca_plot ethylene.gbw -i
mv ethylene.mo8a.cube lumo.cube
```

(menú: 2 = nº de orbital · 4 = grid 80³ · 11 = generar · 12 = salir)


### 3 · Renderizar con el plugin VMD-Tachyon Export

1. Abre MoleditPy y carga la geometría o el cube
   (**Archivo → Abrir** `homo.cube` — el plugin detecta que es un cube y activa la isosuperficie automáticamente).
2. **Plugins → VMD-Tachyon Export** (o el botón *Export 3D*).
3. En la pestaña **Volumetric**: verifica *Isovalue +* **0.05** / *−* **−0.05** y los colores azul/rojo; repite con `lumo.cube` para el LUMO
4. **Render** → imagen en `VMD_Renders/molecule_00X/output/`.
   Cada render deja además la escena editable (`scene/scene.tcl`,
   `scene.vmd`) para retocarla en VMD y volver a renderizar.

### Alternativa: un solo comando

```bash
<moleditpy-venv>/bin/python examples/generate_examples.py
```
> **Numeración de orbitales**: ORCA (y su herramienta `orca_plot`)
> numeran los orbitales **desde 0** — el HOMO de una molécula de capa
> cerrada con 2N electrones aparece como `NO = N-1` en la tabla
> ORBITAL ENERGIES del `.out`. Multiwfn, en cambio, usa base-1 sobre el
> archivo molden. Con `orca_plot` usa el número tal como aparece en la
> tabla de ORCA.