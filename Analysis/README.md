# Türkiye Network–CGE Sector-Prioritization Analysis

This directory contains the reproducible analytical workflow, model inputs,
intermediate datasets, tables, and figures used to construct the Structural
Network Importance Index (SNII), the Economy-wide Impact Performance Index
(EIPI), the empirical hypothesis tests, and the robust-core and policy analyses
for 62 sectors in Türkiye.

## Analysis Pipeline

### `00_Methodology_and_Inputs`

Contains the methodological workflow, sector mapping, model diagram, and source
documentation used to define the common sectoral universe.

### `01_SNII`

Constructs production-network indicators and the four-dimensional SNII. The
main index balances linkage strength, systemic embeddedness, bridging, and
supply–distribution orientation. Entropy-TOPSIS, PCA, and average variants are
retained for robustness analysis.

Key files include:

- `NetworkCalculations.py`: production-network and centrality calculations.
- `SNIIIndex.py`: composite-index construction and diagnostics.
- `SNII_Analysis.ipynb`: executable analysis notebook.
- `3_snii_results.xlsx`: sector-level SNII results and variants.

### `02_CGE_EIPI`

Runs the SAM-calibrated CGE model and constructs EIPI from the outcomes of 62
separate 10% sectoral TFP shocks. The main EIPI balances four channels: growth,
welfare, external balance, and price stability. The main and robustness
variants use the same OLS size-control procedure based on log gross output.

Key files include:

- `CGEAnalysis.py`: baseline and counterfactual CGE solution routines.
- `EIPIIndex.py`: channel construction, alternative composite indices, and
  size adjustment.
- `EIPI_Analysis.ipynb`: full CGE/EIPI workflow.
- `4_eipi_results.xlsx`: raw and size-controlled EIPI variants.
- `GenerateSizeDiagnosticFigure.py`: reproduces the before/after OLS
  size-diagnostic figure, including regression statistics.
- `RunHeterogeneousSensitivity.py`: heterogeneous-elasticity and alternative
  shock/closure checks.

### `03_Hypothesis_Tests`

Merges SNII and EIPI results into the common sector panel and evaluates the
alignment and classification-divergence hypotheses. It contains the headline
correlations, mean-threshold classification, channel-level mechanism checks,
and supporting robustness outputs.

Key files include:

- `StrategicAnalysis.py`: merged-panel tests and classification analysis.
- `Strategic_Hypothesis_Analysis.ipynb`: executable hypothesis-test notebook.
- `1_merged_index_panel.xlsx`: common sector-level analytical panel.
- `PlotStrategicFigure.py`: reproduces the strategic-classification figure.

### `04_Robust_Core_and_Policy`

Applies Pareto dominance, two-dimensional balance rules, and 96 alternative
specifications to distinguish the initial Strategic Core candidates from the
Robust Structural Core. It also evaluates four explicit policy scenarios and
10,000 random five-component policy-weight vectors drawn from a symmetric
Dirichlet distribution.

Key files include:

- `00_RobustCoreAnalysis.py`: end-to-end robust-core and policy analysis.
- `5_pareto_and_balanced_scores.xlsx`: Pareto layers and balance measures.
- `6_specification_robustness.xlsx`: membership results across 96
  specifications.
- `7_normative_policy_scenarios.xlsx`: explicit scenarios and random-weight
  results.
- `14_Robust_Core_Analysis_Report.md`: methodological and empirical summary.
- `15_run_audit.json`: machine-readable run audit.

### `05_Elasticity_Sensitivity`

Contains the CET–Armington and CES elasticity sensitivity analyses used in the
appendix, together with the executable CGE model, SAM input, and exported
results.

## Reproduction

Run commands from the repository root. The notebooks reproduce the complete
SNII, EIPI, and hypothesis-test stages. The following scripts reproduce the
standalone downstream analyses and figures:

```powershell
python "Analysis/02_CGE_EIPI/GenerateSizeDiagnosticFigure.py"
python "Analysis/02_CGE_EIPI/RunHeterogeneousSensitivity.py"
python "Analysis/03_Hypothesis_Tests/PlotStrategicFigure.py"
python "Analysis/04_Robust_Core_and_Policy/00_RobustCoreAnalysis.py"
python "Analysis/05_Elasticity_Sensitivity/CGE_Sensitivity_Analysis.py"
```

The numerical CGE solution requires the Python scientific stack used by the
scripts, together with Pyomo and an accessible IPOPT solver. Spreadsheet and
figure outputs are written to their respective analysis directories.

## Interpretation Notes

- SNII represents structural position and propagation capacity in the
  production network; EIPI represents multichannel general-equilibrium effects.
- Size-controlled EIPI is the min–max-normalized residual from regressing each
  EIPI variant on log gross output. Raw EIPI and scale measures are retained.
- Robust-core membership across 96 specifications is a specification-stability
  frequency, not a sampling probability.
- Random Dirichlet weights represent uncertainty about policy preferences, not
  statistical uncertainty in the data.
- Generated caches and execution logs are intentionally excluded from the
  versioned analysis archive.

## Archive Status

The versioned archive contains 75 files across the six analysis stages,
including source code, notebooks, model inputs, machine-readable outputs, and
publication-ready figures.
