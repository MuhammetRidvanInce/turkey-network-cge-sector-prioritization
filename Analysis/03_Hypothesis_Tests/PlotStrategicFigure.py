from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent

panel = pd.read_excel(BASE / "1_merged_index_panel.xlsx", sheet_name="merged_panel")
classes = pd.read_excel(BASE / "2_strategic_classification_table.xlsx", sheet_name="classification_table")
data = panel.merge(classes[["sector", "category_main_mean"]], on="sector", how="left")

x_col = "EIPI_CH_size_controlled"
y_col = "SNII_DIM"
x_mean = data[x_col].mean()
y_mean = data[y_col].mean()
x_band = 0.05 * (data[x_col].max() - data[x_col].min())
y_band = 0.05 * (data[y_col].max() - data[y_col].min())

colors = {
    "Emerging Leverage Sectors": "#7570b3",
    "Peripheral Limited-Impact Sectors": "#a6a6a6",
    "Strategic Core Sectors": "#1b9e77",
    "Structurally Critical but Low-Transmission Sectors": "#d95f02",
}

fig, ax = plt.subplots(figsize=(12, 8))
ax.axvspan(x_mean - x_band, x_mean + x_band, color="grey", alpha=0.12, zorder=0)
ax.axhspan(y_mean - y_band, y_mean + y_band, color="grey", alpha=0.12, zorder=0)

for category, color in colors.items():
    part = data[data["category_main_mean"] == category]
    ax.scatter(
        part[x_col], part[y_col], s=70, color=color, alpha=0.88,
        edgecolors="black", linewidths=0.8, label=category, zorder=3,
    )

ax.axvline(x_mean, color="black", linestyle="--", linewidth=1.5)
ax.axhline(y_mean, color="black", linestyle="--", linewidth=1.5)

offsets = {
    "sec5": (6, 6), "sec9": (4, 6), "sec29": (7, 6),
    "sec30": (7, 6), "sec31": (-25, -14), "sec38": (7, -14),
    "sec43": (-28, 8), "sec44": (7, 6), "sec49": (7, -14),
}
core = data[data["category_main_mean"] == "Strategic Core Sectors"]
for _, row in core.iterrows():
    ax.annotate(
        row["sector"], (row[x_col], row[y_col]),
        xytext=offsets.get(row["sector"], (6, 6)), textcoords="offset points",
        fontsize=12, fontweight="bold",
    )

ax.set_xlabel(x_col)
ax.set_ylabel(y_col)
ax.set_xlim(-0.05, 1.05)
ax.set_ylim(-0.05, 1.07)
ax.legend(loc="lower right", fontsize=13)
fig.tight_layout()
fig.savefig(ROOT / "figures_en" / "fig4_strategic_classification.png", dpi=300, bbox_inches="tight")
plt.close(fig)
