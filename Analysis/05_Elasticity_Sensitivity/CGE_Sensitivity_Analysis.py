"""Reproduce Appendix C around heterogeneous sector elasticities.

Runs separate 10% sectoral TFP shocks for the three largest and three
smallest sectors by benchmark gross output. Trade and value-added
elasticities are perturbed proportionally around each sector's own
Agriculture/Industry/Service baseline.
"""

from pathlib import Path
import logging
import sys

import pandas as pd


HERE = Path(__file__).resolve().parent
EIPI_DIR = HERE.parent / "02_CGE_EIPI"
sys.path.insert(0, str(EIPI_DIR))

from CGEAnalysis import CGEModel  # noqa: E402


SAM_PATH = EIPI_DIR / "CGEModel-SAM.xlsx"
BASE = {
    parameter: CGEModel.default_elasticity_map(parameter)
    for parameter in ("psi", "sigma", "omega")
}


def scaled(parameter, factor):
    return {sector: value * factor for sector, value in BASE[parameter].items()}


def selected_sectors():
    model = CGEModel(SAM_PATH)
    model.calibrate()
    model.solve_model()
    output = model.baseline_output
    return list(output.nlargest(3).index) + list(output.nsmallest(3).index)


def run_family(family, scenarios, sectors):
    rows = []
    for scenario, parameters in scenarios.items():
        model = CGEModel(SAM_PATH, **parameters)
        model.calibrate()
        baseline = model.solve_model()
        results = model.counterfactual_result(shock_size=0.10, sectors=sectors)
        statuses = results.attrs.get("solver_statuses", {})
        for sector in sectors:
            status, termination = statuses[sector]
            rows.append({
                "experiment": family,
                "scenario": scenario,
                "sector": sector,
                "category": CGEModel.SECTOR_CATEGORIES[sector],
                "psi": parameters["psi"][sector],
                "sigma": parameters["sigma"][sector],
                "omega": parameters["omega"][sector],
                "GDP_pct_change": results.loc[sector, "GDP_pct_change"],
                "Welfare_pct_change": results.loc[sector, "Welfare_pct_change"],
                "Spillover_pct_change": results.loc[sector, "Spillover_pct_change"],
                "solver_status": status,
                "termination_condition": termination,
                "baseline_termination": baseline["solver_termination"],
            })
    return rows


def main():
    logging.getLogger("pyomo").setLevel(logging.ERROR)
    sectors = selected_sectors()
    trade = {
        "low_minus_20pct": {**BASE, "psi": scaled("psi", 0.8), "sigma": scaled("sigma", 0.8)},
        "baseline": BASE,
        "high_plus_20pct": {**BASE, "psi": scaled("psi", 1.2), "sigma": scaled("sigma", 1.2)},
    }
    ces = {
        "low_minus_20pct": {**BASE, "omega": scaled("omega", 0.8)},
        "baseline": BASE,
        "high_plus_20pct": {**BASE, "omega": scaled("omega", 1.2)},
    }
    rows = run_family("CET_Armington_joint", trade, sectors)
    rows += run_family("CES_value_added", ces, sectors)
    frame = pd.DataFrame(rows)
    frame.to_csv(HERE / "cge_elasticity_sensitivity_results.csv", index=False)
    with pd.ExcelWriter(HERE / "cge_elasticity_sensitivity_results.xlsx", engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="all_results", index=False)
        frame[frame["experiment"] == "CET_Armington_joint"].to_excel(
            writer, sheet_name="Table_C1", index=False
        )
        frame[frame["experiment"] == "CES_value_added"].to_excel(
            writer, sheet_name="Table_C2", index=False
        )
    print(frame.to_string(index=False))


if __name__ == "__main__":
    main()
