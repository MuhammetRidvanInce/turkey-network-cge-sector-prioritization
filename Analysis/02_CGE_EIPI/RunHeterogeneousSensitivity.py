"""Regenerate EIPI sensitivity results around heterogeneous sector elasticities."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd
from scipy.stats import spearmanr

from CGEAnalysis import CGEModel
from EIPIIndex import EIPIComposite


BASE = Path(__file__).resolve().parent
SAM_FILE = BASE / "CGEModel-SAM.xlsx"
PRIMARY = "EIPI_CH_size_controlled"


def scaled_default(parameter: str, factor: float) -> dict[str, float]:
    return {
        sector: value * factor
        for sector, value in CGEModel.default_elasticity_map(parameter).items()
    }


def quartiles(series: pd.Series) -> pd.Series:
    return pd.qcut(series.rank(method="first"), 4, labels=False) + 1


def markdown_table(frame: pd.DataFrame) -> str:
    def render(value):
        return f"{value:.4f}" if isinstance(value, float) else str(value)

    header = "| " + " | ".join(frame.columns) + " |"
    separator = "|" + "|".join("---" for _ in frame.columns) + "|"
    rows = [
        "| " + " | ".join(render(value) for value in row) + " |"
        for row in frame.itertuples(index=False, name=None)
    ]
    return "\n".join([header, separator, *rows])


def write_report(summary: pd.DataFrame) -> None:
    report_lines = [
        "# EIPI Duyarlılık Raporu — Heterojen Sektörel Esneklikler",
        "",
        "Karşılaştırmaların tamamında birincil ölçüt `EIPI_CH_size_controlled` kullanılmıştır.",
        "Ana modelde esneklikler Agriculture, Industry ve Service gruplarına göre sektör bazında atanmıştır.",
        "",
        markdown_table(summary),
        "",
        "`sigma_plus25` senaryosunda hizmet sektörleri için sigma tam olarak 1'e ulaştığından,",
        "Armington bileşimi CES'in Cobb–Douglas sınırı kullanılarak çözülmüştür.",
    ]
    (BASE / "EIPI_Duyarlilik_Raporu.md").write_text("\n".join(report_lines), encoding="utf-8")


def solve_scenario(name: str, model_kwargs: dict, shock_size) -> tuple[pd.DataFrame, dict]:
    started = time.perf_counter()
    model = CGEModel(SAM_FILE, **model_kwargs)
    model.calibrate()
    baseline = model.solve_model()
    counterfactual = model.counterfactual_result(shock_size=shock_size)
    eipi = EIPIComposite(counterfactual, sector_output=model.baseline_output)
    result = eipi.general_results()

    pct_cols = [c for c in counterfactual.columns if c.endswith("_pct_change")]
    detail = result.join(counterfactual[pct_cols])
    statuses = counterfactual.attrs.get("solver_statuses", {})
    success = sum(
        str(termination).lower() in {"optimal", "locallyoptimal", "feasible"}
        for _, termination in statuses.values()
    )
    meta = {
        "name": name,
        "success": success,
        "total": len(counterfactual),
        "seconds": time.perf_counter() - started,
        "baseline_termination": baseline["solver_termination"],
    }
    return detail, meta


def main() -> None:
    logging.getLogger("pyomo").setLevel(logging.ERROR)

    base_counterfactual = pd.read_excel(
        BASE / "2_cge_counterfactual_results.xlsx",
        sheet_name="counterfactual_10pct",
        index_col=0,
    )
    baseline_result = pd.read_excel(
        BASE / "4_eipi_results.xlsx", sheet_name="eipi_results", index_col=0
    )
    base_primary = baseline_result[PRIMARY]
    base_top = set(base_primary.nlargest(10).index)
    base_bottom = set(base_primary.nsmallest(10).index)
    base_quartiles = quartiles(base_primary)

    base_model = CGEModel(SAM_FILE)
    base_model.calibrate()
    base_model.solve_model()
    mean_output = base_model.baseline_output.mean()
    size_normalized = (
        0.10 * mean_output / base_model.baseline_output
    ).clip(lower=0.02, upper=0.50).to_dict()

    defaults = {
        parameter: CGEModel.default_elasticity_map(parameter)
        for parameter in ("omega", "psi", "sigma")
    }
    scenarios = [
        ("shock_5pct", "TFP shock: 5%", {}, 0.05),
        ("shock_20pct", "TFP shock: 20%", {}, 0.20),
        ("size_normalized", "Output-size-normalized TFP shock", {}, size_normalized),
        ("omega_plus25", "Sector-specific omega +25%", {
            "omega": scaled_default("omega", 1.25), "psi": defaults["psi"], "sigma": defaults["sigma"]}, 0.10),
        ("omega_minus25", "Sector-specific omega -25%", {
            "omega": scaled_default("omega", 0.75), "psi": defaults["psi"], "sigma": defaults["sigma"]}, 0.10),
        ("psi_plus25", "Sector-specific psi +25%", {
            "omega": defaults["omega"], "psi": scaled_default("psi", 1.25), "sigma": defaults["sigma"]}, 0.10),
        ("psi_minus25", "Sector-specific psi -25%", {
            "omega": defaults["omega"], "psi": scaled_default("psi", 0.75), "sigma": defaults["sigma"]}, 0.10),
        ("sigma_plus25", "Sector-specific sigma +25%", {
            "omega": defaults["omega"], "psi": defaults["psi"], "sigma": scaled_default("sigma", 1.25)}, 0.10),
        ("sigma_minus25", "Sector-specific sigma -25%", {
            "omega": defaults["omega"], "psi": defaults["psi"], "sigma": scaled_default("sigma", 0.75)}, 0.10),
        ("fixed_wage_closure", "Fixed-wage numeraire", {"numeraire": "fixed_wage"}, 0.10),
    ]

    details = {}
    summary_rows = []
    for name, description, kwargs, shock in scenarios:
        detail, meta = solve_scenario(name, kwargs, shock)
        details[name] = detail
        candidate = detail[PRIMARY]
        corr = spearmanr(base_primary, candidate)
        summary_rows.append({
            "Scenario": name,
            "Description": description,
            "Successful solutions": f"{meta['success']}/{meta['total']}",
            "Baseline termination": meta["baseline_termination"],
            "Spearman rho": corr.statistic,
            "p-value": corr.pvalue,
            "Top-10 overlap": f"{len(base_top & set(candidate.nlargest(10).index))}/10",
            "Bottom-10 overlap": f"{len(base_bottom & set(candidate.nsmallest(10).index))}/10",
            "Quartile changes": int((base_quartiles != quartiles(candidate)).sum()),
            "Runtime seconds": meta["seconds"],
        })
        print(name, summary_rows[-1])

    summary = pd.DataFrame(summary_rows)
    elasticity_assignment = pd.DataFrame({
        "category": pd.Series(CGEModel.SECTOR_CATEGORIES),
        "psi_CET": pd.Series(defaults["psi"]),
        "sigma_Armington": pd.Series(defaults["sigma"]),
        "omega_CES": pd.Series(defaults["omega"]),
    }).sort_index(key=lambda idx: idx.str.extract(r"(\d+)", expand=False).astype(int))

    output = BASE / "5_eipi_sensitivity_results.xlsx"
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        elasticity_assignment.to_excel(writer, sheet_name="Elasticity_Assignment")
        base_counterfactual.to_excel(writer, sheet_name="Baseline_CGE_10pct")
        for name, detail in details.items():
            detail.to_excel(writer, sheet_name=name[:31])

    write_report(summary)


if __name__ == "__main__":
    main()
