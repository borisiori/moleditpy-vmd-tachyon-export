# Examples gallery

Nine reproducible workflows showing what the plugin renders. Each
folder has the input files and a step-by-step guide — the general
pattern is always the same: **select the analysis, load the file(s),
press Render**.

| # | Analysis | Molecule | Guide |
|---|---|---|---|
| 00 | Quickstart | Ethylene HOMO | [guide](00_quickstart/README.md) |
| 01 | HOMO / LUMO | Ethylene, Formaldehyde | [ethylene](01_orbitals/ethylene/README.md) · [formaldehyde](01_orbitals/formaldehyde/README.md) |
| 02 | MEP | Water, Chloromethane | [water](02_mep/water/README.md) · [chloromethane](02_mep/chloromethane/README.md) |
| 03 | Spin + ELF | Methyl radical, Water | [spin](03_spin_elf/methyl_radical/README.md) · [ELF](03_spin_elf/water_elf/README.md) |
| 04 | NCI | Phenol dimer | [guide](04_nci/phenol_dimer/README.md) |
| 05 | QTAIM | Phenol dimer | [guide](05_qtaim/phenol_dimer/README.md) |
| 06 | Dual Descriptor | Phenol | [guide](06_dual_descriptor/phenol/README.md) |

## The general workflow

1. Open MoleditPy, load your molecule (or just open a `.cub`/`.cube`
   file — the plugin auto-detects it as volumetric data)
2. **Plugins → VMD-Tachyon Export**
3. Pick an **Analysis** from the dropdown (NCI, Orbital, MEP, QTAIM, …)
4. Load the required files:
   - Cube analyses: **Load Cube…** (and **Load Map…** for MEP/NCI coloring)
   - QTAIM: **CPs…** and **Paths…** in the QTAIM tab
5. Press **Render**

Each render produces an editable VMD scene project (`scene.tcl`,
`scene.vmd`, re-render scripts, `open_in_vmd.sh` launcher).
