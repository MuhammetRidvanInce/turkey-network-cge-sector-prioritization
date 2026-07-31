from __future__ import annotations

import itertools
import json
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
import numpy as np
import pandas as pd
import seaborn as sns


BASE = Path(__file__).resolve().parent
SEED = 20260715
RNG = np.random.default_rng(SEED)

SNII_VARIANTS = ["SNII_DIM", "SNII_TOPSIS", "SNII_PCA", "SNII_AVG"]
EIPI_VARIANTS = [
    "EIPI_CH_size_controlled",
    "EIPI_TOPSIS_size_controlled",
    "EIPI_PCA_size_controlled",
    "EIPI_AVG_size_controlled",
]
TRANSFORMS = ["percentile", "minmax"]
BALANCE_RULES = ["geometric", "bottleneck", "hybrid"]

CHANNELS = [
    "SNII_DIM",
    "C1_growth_spillover",
    "C2_welfare_distribution",
    "C3_external_balance",
    "C4_price_stability",
]

SCENARIO_WEIGHTS = {
    "Balanced": [0.20, 0.20, 0.20, 0.20, 0.20],
    "Growth_oriented": [0.20, 0.35, 0.20, 0.15, 0.10],
    "Price_stability": [0.15, 0.15, 0.20, 0.15, 0.35],
    "Network_resilience": [0.40, 0.15, 0.15, 0.20, 0.10],
}


def percentile_rank(series: pd.Series) -> np.ndarray:
    return series.rank(method="average", pct=True).to_numpy(dtype=float)


def minmax(series: pd.Series) -> np.ndarray:
    values = series.to_numpy(dtype=float)
    spread = np.nanmax(values) - np.nanmin(values)
    if spread <= 0:
        return np.full(len(values), 0.5)
    return (values - np.nanmin(values)) / spread


def transform(series: pd.Series, method: str) -> np.ndarray:
    if method == "percentile":
        return percentile_rank(series)
    if method == "minmax":
        return minmax(series)
    raise ValueError(method)


def pareto_layers(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Non-dominated sorting for a two-objective maximization problem."""
    n = len(x)
    remaining = set(range(n))
    layers = np.zeros(n, dtype=int)
    layer = 1
    while remaining:
        front = []
        for i in remaining:
            dominated = any(
                j != i
                and x[j] >= x[i]
                and y[j] >= y[i]
                and (x[j] > x[i] or y[j] > y[i])
                for j in remaining
            )
            if not dominated:
                front.append(i)
        if not front:
            raise RuntimeError("Pareto sorting could not identify a front.")
        for i in front:
            layers[i] = layer
        remaining.difference_update(front)
        layer += 1
    return layers


def balanced_score(x: np.ndarray, y: np.ndarray, rule: str) -> np.ndarray:
    geometric = np.sqrt(np.clip(x, 0, None) * np.clip(y, 0, None))
    bottleneck = np.minimum(x, y)
    if rule == "geometric":
        return geometric
    if rule == "bottleneck":
        return bottleneck
    if rule == "hybrid":
        return 0.5 * geometric + 0.5 * bottleneck
    raise ValueError(rule)


def robust_label(rate: float) -> str:
    if rate >= 0.80:
        return "Robust Core"
    if rate >= 0.60:
        return "Probable Core"
    if rate >= 0.40:
        return "Specification-Sensitive"
    return "Not Robust Core"


def write_excel(path: Path, sheets: dict[str, pd.DataFrame]) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name[:31], index=False)


def load_and_validate() -> pd.DataFrame:
    panel = pd.read_excel(BASE / "1_input_merged_index_panel.xlsx")
    required = {"sector", *SNII_VARIANTS, *EIPI_VARIANTS, *CHANNELS}
    missing = sorted(required.difference(panel.columns))
    if missing:
        raise KeyError(f"Missing required columns: {missing}")
    if len(panel) != 62 or panel["sector"].nunique() != 62:
        raise ValueError("Expected 62 unique sectors.")
    if panel[list(required - {"sector"})].isna().any().any():
        raise ValueError("Analysis columns contain missing values.")
    return panel.sort_values("sector", key=lambda s: s.str.extract(r"(\d+)")[0].astype(int)).reset_index(drop=True)


def primary_analysis(panel: pd.DataFrame) -> pd.DataFrame:
    s = percentile_rank(panel["SNII_DIM"])
    e = percentile_rank(panel["EIPI_CH_size_controlled"])
    layers = pareto_layers(s, e)
    geometric = balanced_score(s, e, "geometric")
    bottleneck = balanced_score(s, e, "bottleneck")
    hybrid = balanced_score(s, e, "hybrid")
    cutoff = float(np.quantile(hybrid, 0.75))
    out = pd.DataFrame({
        "sector": panel["sector"],
        "SNII_percentile": s,
        "EIPI_percentile": e,
        "pareto_layer": layers,
        "pareto_front_1": layers == 1,
        "geometric_balance": geometric,
        "bottleneck_balance": bottleneck,
        "hybrid_balance": hybrid,
        "hybrid_q75_cutoff": cutoff,
        "primary_core_candidate": (layers == 1) & (hybrid >= cutoff),
    })
    return out.sort_values(["pareto_layer", "hybrid_balance"], ascending=[True, False])


def specification_analysis(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    records = []
    spec_id = 0
    for sn, ep, tr, rule in itertools.product(
        SNII_VARIANTS, EIPI_VARIANTS, TRANSFORMS, BALANCE_RULES
    ):
        spec_id += 1
        x = transform(panel[sn], tr)
        y = transform(panel[ep], tr)
        layers = pareto_layers(x, y)
        score = balanced_score(x, y, rule)
        cutoff = float(np.quantile(score, 0.75))
        selected = (layers == 1) & (score >= cutoff)
        for idx, sector in enumerate(panel["sector"]):
            records.append({
                "specification_id": spec_id,
                "sector": sector,
                "snii_variant": sn,
                "eipi_variant": ep,
                "transform": tr,
                "balance_rule": rule,
                "snii_value": x[idx],
                "eipi_value": y[idx],
                "pareto_layer": layers[idx],
                "balanced_score": score[idx],
                "q75_cutoff": cutoff,
                "selected_core": bool(selected[idx]),
            })
    runs = pd.DataFrame(records)
    summary = (
        runs.groupby("sector", as_index=False)
        .agg(
            core_selection_count=("selected_core", "sum"),
            specification_count=("selected_core", "size"),
            mean_pareto_layer=("pareto_layer", "mean"),
            median_pareto_layer=("pareto_layer", "median"),
            front_1_frequency=("pareto_layer", lambda x: float(np.mean(np.asarray(x) == 1))),
            mean_balanced_score=("balanced_score", "mean"),
        )
    )
    summary["core_selection_rate"] = summary["core_selection_count"] / summary["specification_count"]
    summary["robust_core_class"] = summary["core_selection_rate"].map(robust_label)
    summary = summary.sort_values(
        ["core_selection_rate", "mean_balanced_score"], ascending=[False, False]
    )
    method_summary = (
        runs.groupby(["snii_variant", "eipi_variant", "transform", "balance_rule"], as_index=False)
        .agg(n_selected=("selected_core", "sum"), n_front_1=("pareto_layer", lambda x: int(np.sum(np.asarray(x) == 1))))
    )
    return runs, summary, method_summary


def normative_analysis(panel: pd.DataFrame, robustness: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    ranks = pd.DataFrame({"sector": panel["sector"]})
    for col in CHANNELS:
        ranks[f"rank_{col}"] = percentile_rank(panel[col])

    long_records = []
    for scenario, weights in SCENARIO_WEIGHTS.items():
        matrix = ranks[[f"rank_{c}" for c in CHANNELS]].to_numpy()
        score = matrix @ np.asarray(weights)
        order = pd.Series(score).rank(method="min", ascending=False).astype(int)
        for i, sector in enumerate(panel["sector"]):
            long_records.append({
                "scenario": scenario,
                "sector": sector,
                "normative_score": score[i],
                "rank": int(order.iloc[i]),
                "top_10": bool(order.iloc[i] <= 10),
            })
    scenario_long = pd.DataFrame(long_records)

    top10 = scenario_long[scenario_long["top_10"]].sort_values(["scenario", "rank"])
    consensus = (
        scenario_long.groupby("sector", as_index=False)
        .agg(
            top10_scenario_count=("top_10", "sum"),
            mean_normative_rank=("rank", "mean"),
            best_normative_rank=("rank", "min"),
            worst_normative_rank=("rank", "max"),
        )
        .merge(robustness[["sector", "core_selection_rate", "robust_core_class"]], on="sector", how="left")
    )

    # Preference uncertainty, not sampling uncertainty: random weights on the five-simplex.
    n_draws = 10_000
    random_weights = RNG.dirichlet(np.ones(len(CHANNELS)), size=n_draws)
    matrix = ranks[[f"rank_{c}" for c in CHANNELS]].to_numpy()
    scores = matrix @ random_weights.T
    top10_counts = np.zeros(len(panel), dtype=int)
    winner_counts = np.zeros(len(panel), dtype=int)
    for draw in range(n_draws):
        order = np.argsort(-scores[:, draw], kind="mergesort")
        top10_counts[order[:10]] += 1
        winner_counts[order[0]] += 1
    monte_carlo = pd.DataFrame({
        "sector": panel["sector"],
        "top10_preference_frequency": top10_counts / n_draws,
        "rank1_preference_frequency": winner_counts / n_draws,
        "n_weight_draws": n_draws,
    }).sort_values("top10_preference_frequency", ascending=False)
    consensus = consensus.merge(monte_carlo, on="sector", how="left").sort_values(
        ["top10_scenario_count", "top10_preference_frequency", "core_selection_rate"],
        ascending=[False, False, False],
    )

    weights = pd.DataFrame(SCENARIO_WEIGHTS, index=CHANNELS).T.reset_index(names="scenario")
    return scenario_long, top10, consensus, weights


def comparison_table(panel: pd.DataFrame, primary: pd.DataFrame, robustness: pd.DataFrame, consensus: pd.DataFrame) -> pd.DataFrame:
    old = pd.read_excel(BASE / "2_input_strategic_classification_table.xlsx", sheet_name="classification_table")
    out = (
        panel[["sector", "SNII_DIM", "EIPI_CH_size_controlled"]]
        .merge(old[["sector", "category_main_mean", "borderline"]], on="sector", how="left")
        .merge(primary, on="sector", how="left")
        .merge(robustness, on="sector", how="left")
        .merge(consensus, on=["sector", "core_selection_rate", "robust_core_class"], how="left")
    )
    out["final_robust_core"] = (
        out["primary_core_candidate"]
        & (out["core_selection_rate"] >= 0.80)
    )
    out["method_transition"] = np.where(
        (out["category_main_mean"] == "Strategic Core Sectors") & out["final_robust_core"],
        "Core in both methods",
        np.where(
            (out["category_main_mean"] == "Strategic Core Sectors") & ~out["final_robust_core"],
            "Mean-core, not robust-core",
            np.where(out["final_robust_core"], "New robust-core", "Non-core in robust method"),
        ),
    )
    return out.sort_values(
        ["final_robust_core", "core_selection_rate", "hybrid_balance"], ascending=[False, False, False]
    )


def figures(primary: pd.DataFrame, robustness: pd.DataFrame, scenario_long: pd.DataFrame, consensus: pd.DataFrame, comparison: pd.DataFrame) -> None:
    sns.set_theme(style="whitegrid", context="talk")

    fig, ax = plt.subplots(figsize=(11, 8))

    selected_layers = [1, 2, 3, 9, 10, 11, 12]
    selected = primary[primary["pareto_layer"].isin(selected_layers)]
    other_sectors = primary[~primary["pareto_layer"].isin(selected_layers)]
    ax.scatter(
        other_sectors["EIPI_percentile"], other_sectors["SNII_percentile"],
        s=60, c="#3182bd", alpha=0.30, label="Other sectors", edgecolors="none",
    )

    layer_colors = {
        1: "#d73027", 2: "#f46d43", 3: "#fdae61",
        9: "#4575b4", 10: "#66bd63", 11: "#984ea3", 12: "#252525",
    }
    layer_markers = {1: "o", 2: "^", 3: "D", 9: "P", 10: "s", 11: "v", 12: "X"}
    layer_linestyles = {1: "--", 2: "-.", 3: ":", 9: "-.", 10: "--", 11: ":", 12: "-"}

    for layer in [1, 2, 3, 9, 10, 11, 12]:
        layer_data = selected[selected["pareto_layer"] == layer].sort_values("EIPI_percentile")
        ax.scatter(
            layer_data["EIPI_percentile"], layer_data["SNII_percentile"], s=90,
            c=layer_colors[layer], marker=layer_markers[layer], alpha=0.92,
            label=f"Pareto Layer {layer}", edgecolors="k", linewidths=0.75,
        )
        ax.plot(
            layer_data["EIPI_percentile"], layer_data["SNII_percentile"],
            color=layer_colors[layer], linestyle=layer_linestyles[layer],
            lw=1.55, label="_nolegend_",
        )

    label_offsets = {
        "sec15": (6, -14), "sec24": (6, 7),
        "sec11": (-10, 9), "sec54": (8, -7),
        "sec19": (7, -13), "sec56": (7, 8),
    }
    for _, row in selected.iterrows():
        ax.annotate(
            row["sector"], (row["EIPI_percentile"], row["SNII_percentile"]),
            xytext=label_offsets.get(row["sector"], (5, 5)),
            textcoords="offset points", fontsize=8.3,
            weight="bold" if row["pareto_layer"] in {1, 12} else "normal",
        )

    ax.set(
        xlabel="EIPI percentile rank", ylabel="SNII percentile rank",
        title="Selected Pareto fronts (Layers 1–3 and 9–12)",
    )
    ax.set_xlim(-0.02, 1.05)
    ax.set_ylim(-0.02, 1.05)
    ax.legend(fontsize=8.4, loc="lower right", ncol=2)
    fig.tight_layout()
    fig.savefig(BASE / "9_primary_pareto_core.png", dpi=300)
    plt.close(fig)

    n_zero = int((robustness["core_selection_rate"] == 0).sum())
    plot = robustness[robustness["core_selection_rate"] > 0].sort_values("core_selection_rate", ascending=True)
    fig, ax = plt.subplots(figsize=(10, 6))
    palette = plot["robust_core_class"].map({
        "Robust Core": "#B2182B", "Probable Core": "#EF8A62",
        "Specification-Sensitive": "#FDB863", "Not Robust Core": "#67A9CF"
    })
    ax.barh(plot["sector"], plot["core_selection_rate"], color=palette)
    ax.axvline(0.80, color="#B2182B", ls="--", lw=1.4, label="Robust-core threshold")
    ax.axvline(0.60, color="#EF8A62", ls=":", lw=1.2)
    ax.axvline(0.40, color="#FDB863", ls=":", lw=1.2)
    ax.set(xlabel="Core selection rate across 96 specifications", ylabel="Sector",
           title=f"Specification robustness of core membership ({n_zero} sectors never selected)", xlim=(0, 1.02))
    ax.legend(fontsize=10); fig.tight_layout()
    fig.savefig(BASE / "10_specification_robustness.png", dpi=300); plt.close(fig)

    pivot = scenario_long.pivot(index="sector", columns="scenario", values="rank")
    ordered = consensus.sort_values("mean_normative_rank").head(20)["sector"]
    pivot = pivot.loc[ordered]
    fig, ax = plt.subplots(figsize=(9, 8))
    sns.heatmap(pivot, cmap="viridis_r", annot=True, fmt=".0f", cbar_kws={"label": "Rank"}, ax=ax)
    ax.set(title="Sector ranks under explicit normative policy scenarios", xlabel="Policy scenario", ylabel="Sector")
    fig.tight_layout(); fig.savefig(BASE / "11_normative_scenario_heatmap.png", dpi=300); plt.close(fig)

    top = consensus.head(20).sort_values("top10_preference_frequency")
    fig, ax = plt.subplots(figsize=(10, 8))
    ax.barh(top["sector"], top["top10_preference_frequency"], color="#4D9221")
    ax.set(xlabel="Top-10 frequency across 10,000 random policy-weight draws", ylabel="Sector",
           title="Preference-uncertainty sensitivity of normative prioritization", xlim=(0, 1))
    fig.tight_layout(); fig.savefig(BASE / "12_normative_preference_uncertainty.png", dpi=300); plt.close(fig)


def make_notebook() -> None:
    cells = [
        {"cell_type": "markdown", "metadata": {}, "source": [
            "# Analysis IV — Robust Core Identification\n",
            "This notebook executes the complete Pareto, balance, specification-robustness, and normative-scenario pipeline.\n"
        ]},
        {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": [
            "from pathlib import Path\n",
            "import runpy\n",
            "runpy.run_path(str(Path('00_RobustCoreAnalysis.py').resolve()), run_name='__main__')\n"
        ]},
        {"cell_type": "markdown", "metadata": {}, "source": [
            "## Interpretation guardrail\n",
            "The membership rate is a specification-robustness frequency, not a sampling probability. Random policy-weight draws represent preference uncertainty, not statistical uncertainty.\n"
        ]},
    ]
    notebook = {
        "cells": cells,
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                     "language_info": {"name": "python", "version": "3"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    (BASE / "13_Robust_Core_Analysis.ipynb").write_text(json.dumps(notebook, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    panel = load_and_validate()
    primary = primary_analysis(panel)
    runs, robustness, method_summary = specification_analysis(panel)
    scenario_long, top10, consensus, weights = normative_analysis(panel, robustness)
    comparison = comparison_table(panel, primary, robustness, consensus)

    definitions = pd.DataFrame([
        ["Primary Pareto admissibility", "Pareto layer = 1 using percentile SNII_DIM and size-controlled EIPI"],
        ["High balanced performance", "Hybrid balance >= cross-sector 75th percentile"],
        ["Primary core candidate", "Pareto layer 1 AND high hybrid balance"],
        ["Specification grid", "4 SNII × 4 EIPI × 2 transforms × 3 balance rules = 96"],
        ["Robust Core", "Primary candidate AND core-selection rate >= 0.80"],
        ["Normative analysis", "Five explicit criteria; four declared scenarios and 10,000 simplex weight draws"],
    ], columns=["concept", "operational_definition"])

    write_excel(BASE / "5_pareto_and_balanced_scores.xlsx", {
        "primary_results": primary,
        "pareto_layer_counts": primary.groupby("pareto_layer", as_index=False).size(),
        "definitions": definitions,
    })
    write_excel(BASE / "6_specification_robustness.xlsx", {
        "sector_summary": robustness,
        "specification_runs": runs,
        "method_summary": method_summary,
    })
    write_excel(BASE / "7_normative_policy_scenarios.xlsx", {
        "scenario_weights": weights,
        "scenario_sector_ranks": scenario_long,
        "scenario_top10": top10,
        "consensus_summary": consensus,
    })
    transition_summary = comparison.groupby("method_transition", as_index=False).size()
    write_excel(BASE / "8_classification_comparison.xlsx", {
        "sector_comparison": comparison,
        "transition_summary": transition_summary,
        "final_robust_core": comparison[comparison["final_robust_core"]],
    })
    figures(primary, robustness, scenario_long, consensus, comparison)
    make_notebook()

    audit = {
        "n_sectors": int(len(panel)),
        "n_specifications": int(runs["specification_id"].nunique()),
        "primary_front_1": primary.loc[primary["pareto_front_1"], "sector"].tolist(),
        "primary_core_candidates": primary.loc[primary["primary_core_candidate"], "sector"].tolist(),
        "robust_core": comparison.loc[comparison["final_robust_core"], "sector"].tolist(),
        "random_seed": SEED,
    }
    (BASE / "15_run_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
