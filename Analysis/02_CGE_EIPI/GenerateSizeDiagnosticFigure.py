from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
import statsmodels.api as sm


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main() -> None:
    results = pd.read_excel(
        HERE / "4_eipi_results.xlsx", sheet_name="eipi_results"
    ).set_index("sector")
    output = pd.read_excel(
        HERE / "1_cge_baseline_solution.xlsx",
        sheet_name="SAM_reproduction_check",
    ).set_index("Unnamed: 0")["SAM_benchmark_Z"]

    plot_data = results[["EIPI_CH", "EIPI_CH_size_controlled"]].copy()
    plot_data["log_output"] = np.log(output.reindex(plot_data.index))

    raw_model = sm.OLS(
        plot_data["EIPI_CH"], sm.add_constant(plot_data["log_output"])
    ).fit()
    controlled_model = sm.OLS(
        plot_data["EIPI_CH_size_controlled"],
        sm.add_constant(plot_data["log_output"]),
    ).fit()

    sns.set_theme(style="whitegrid", context="notebook")
    fig, axes = plt.subplots(1, 2, figsize=(16, 6.5))
    panels = [
        ("EIPI_CH", "Raw EIPI and sector size", raw_model),
        (
            "EIPI_CH_size_controlled",
            "EIPI after OLS size control",
            controlled_model,
        ),
    ]

    for ax, (column, title, model) in zip(axes, panels):
        sns.regplot(
            data=plot_data,
            x="log_output",
            y=column,
            ax=ax,
            scatter_kws={"s": 45, "alpha": 0.8},
            line_kws={"linewidth": 2.5},
        )
        slope = model.params["log_output"]
        if column == "EIPI_CH":
            p_value = model.pvalues["log_output"]
            p_text = "$p<0.001$" if p_value < 0.001 else rf"$p={p_value:.3f}$"
            annotation = (
                rf"OLS slope: $\beta={slope:.3f}$" "\n"
                rf"$t={model.tvalues['log_output']:.3f}$, {p_text}" "\n"
                rf"$R^2={model.rsquared:.3f}$"
            )
        else:
            annotation = rf"OLS slope: $\beta={slope:.3f}$"
        ax.text(
            0.04,
            0.94,
            annotation,
            transform=ax.transAxes,
            ha="left",
            va="top",
            fontsize=12,
            bbox={
                "boxstyle": "round,pad=0.3",
                "facecolor": "white",
                "edgecolor": "0.35",
                "alpha": 0.9,
            },
        )
        ax.set_title(title)
        ax.set_xlabel("Log gross output")
        ax.set_ylabel("EIPI_CH_SC" if column == "EIPI_CH_size_controlled" else column)

    fig.tight_layout()
    destinations = [
        HERE / "8_eipi_size_diagnostic.png",
        ROOT / "figures_en" / "fig3_eipi_size_diagnostic.png",
    ]
    for destination in destinations:
        fig.savefig(destination, dpi=300, bbox_inches="tight")
        print(f"Saved: {destination}")
    plt.close(fig)


if __name__ == "__main__":
    main()
