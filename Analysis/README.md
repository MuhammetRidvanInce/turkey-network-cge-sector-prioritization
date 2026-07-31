# Analysis Archive

This directory consolidates the analysis inputs, code, notebooks, intermediate
outputs, final tables, and figures used in `Makale_Taslagi_I.md`. The original
directories have not been deleted or moved; their contents were copied here to
preserve the relative links used in the manuscript.

## Directory Structure

- `00_Methodology_and_Inputs`: methodology, sector mapping, and methodological diagrams.
- `01_SNII`: network indicators, SNII code, intermediate outputs, tables, and figures.
- `02_CGE_EIPI`: SAM/CGE solutions, counterfactual shocks, and EIPI results.
- `03_Hypothesis_Tests`: merged panel, H1–H4 tests, classification, and robustness outputs.
- `04_Robust_Core_and_Policy`: Pareto layers, balanced scores, 96 specifications, and policy scenarios.
- `05_Elasticity_Sensitivity`: CET–Armington and CES sensitivity analyses reported in Appendix C.

## Main Evidence Chain in the Manuscript

1. `01_SNII`: Figures 2–4 and the main SNII tables.
2. `02_CGE_EIPI`: Figures 5–6 and the main CGE/EIPI results.
3. `03_Hypothesis_Tests`: H1–H4 results and Figure 7.
4. `04_Robust_Core_and_Policy`: Figures 8–9, Pareto analysis, robust-core results, and policy results.
5. `05_Elasticity_Sensitivity`: Appendix C, Tables C1–C2, and the reproduction script.

## Reproducing the Sensitivity Analysis

From the project root directory, run:

```powershell
python "Analysis/05_Elasticity_Sensitivity/CGE_Sensitivity_Analysis.py"
```

The script generates the results as CSV and Excel files in the same directory.
The executable model code and SAM input required to reproduce Appendix C are
stored alongside the sensitivity-analysis package in that directory.

## Integrity Check

- Files from all four main stages of the analysis used in the manuscript have been copied in full.
- Directory names do not contain development-tool or model names.
- The archive has been verified to contain all nine principal figures linked in the manuscript.
- The sensitivity reference, reproduction script, CSV output, and three-sheet Excel results file are included.
- The current archive contains 83 files and is approximately 15.79 MB in size.
