import numpy as np
import pandas as pd
from scipy.stats import pearsonr, spearmanr
from sklearn.linear_model import ElasticNetCV, RidgeCV, LinearRegression
from sklearn.metrics import cohen_kappa_score, r2_score
from sklearn.model_selection import LeaveOneOut, cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


PREDICTOR_SETS = {
    "dimension_subscores": ["D1_direct", "D2_embeddedness", "D3_bridge", "D4_supply"],
    "retained_5": ["backward_linkage", "forward_linkage", "pagerank", "betweenness_centrality", "hub_score"],
    "all_7_raw": ["backward_linkage", "forward_linkage", "eigenvector_centrality", "pagerank",
                  "betweenness_centrality", "hub_score", "authority_score"],
    "SNII_DIM_only": ["SNII_DIM"],
}
CGE_OUTCOMES = ["GDP_pct_change", "Welfare_pct_change", "ExportShare_pct_change",
                "LaborIncomeShare_pct_change", "Spillover_pct_change",
                "ImportDependency_pct_change", "CPI_pct_change"]
RAW_NETWORK_INDICATORS = ["backward_linkage", "forward_linkage", "eigenvector_centrality", "pagerank",
                           "betweenness_centrality", "hub_score", "authority_score"]
CATEGORY_LABELS = {
    (True, True): "Strategic Core Sectors",
    (True, False): "Structurally Critical but Low-Transmission Sectors",
    (False, True): "Emerging Leverage Sectors",
    (False, False): "Peripheral Limited-Impact Sectors",
}


class StrategicAnalysis:
    """
    Final analytical layer combining the network-based SNII and the CGE-based
    EIPI. Performs NO new network or CGE computation -- it merges the
    already-produced SNII/EIPI/CGE outputs, builds the four-quadrant
    strategic classification, and runs the H1-H4 hypothesis tests plus
    robustness checks (Strategic_Analysis_Prompt.md).

    Input files (defaults) are this project's own pipeline outputs:
    `3_snii_results.xlsx` (Analiz I -- SNIIComposite.general_results() export),
    `4_eipi_results.xlsx` / `2_cge_counterfactual_results.xlsx` /
    `1_cge_baseline_solution.xlsx` (Analiz II -- CGEModel / EIPIComposite
    exports). The prompt's expected `network_candidate_indicatorsV2.xlsx`
    is not produced by this project's pipeline under that name; the seven
    raw network indicators are instead sourced from
    `../Analiz I/1_network_candidate_indicators.xlsx`
    (`coefficient_based_indicators` sheet) -- the SNII pipeline's own
    raw-indicator export, built from the same `1-IntermediateInputs.xlsx`.
    """

    RANDOM_STATE = 42
    BORDERLINE_BAND_FRACTION = 0.05  # +/- 5% of each axis's range around its threshold

    def __init__(self, snii_path="3_snii_results.xlsx", eipi_path="4_eipi_results.xlsx",
                 network_path="../Analiz I/1_network_candidate_indicators.xlsx",
                 cge_path="2_cge_counterfactual_results.xlsx",
                 io_path="1-IntermediateInputs.xlsx",
                 baseline_path="1_cge_baseline_solution.xlsx",
                 snii_axis="SNII_DIM", eipi_axis="EIPI_CH_size_controlled"):
        self.snii_path = snii_path
        self.eipi_path = eipi_path
        self.network_path = network_path
        self.cge_path = cge_path
        self.io_path = io_path
        self.baseline_path = baseline_path
        self.snii_axis = snii_axis
        self.eipi_axis = eipi_axis

        self.panel = None
        self.classifications = None
        self.classification_meta = None

    # ------------------------------------------------------------------
    # Section: data merging
    # ------------------------------------------------------------------
    def _read_sheet(self, path, preferred_sheets, index_col=0):
        xl = pd.ExcelFile(path)
        for sheet in preferred_sheets:
            if sheet in xl.sheet_names:
                return xl.parse(sheet, index_col=index_col)
        return xl.parse(xl.sheet_names[0], index_col=index_col)

    def build_panel(self) -> pd.DataFrame:
        """Merge SNII, EIPI, raw network indicators, CGE outcomes, and
        sector size (Domar weight, from the IO file's last column -- NOT
        the CGE baseline file) into one 62-row panel, one row per sector."""
        snii = self._read_sheet(self.snii_path, ["selected_set_results", "selected_primary", "selected", "results"])
        snii_cols = ["SNII_DIM", "SNII_TOPSIS", "SNII_PCA", "SNII_AVG",
                     "D1_direct", "D2_embeddedness", "D3_bridge", "D4_supply"]
        snii = snii[[c for c in snii_cols if c in snii.columns]].copy()

        eipi = self._read_sheet(self.eipi_path, ["eipi_results", "eipi_primary", "results"])
        eipi_cols = ["EIPI_CH", "EIPI_CH_size_controlled", "EIPI_TOPSIS_size_controlled",
                     "EIPI_PCA_size_controlled", "EIPI_AVG_size_controlled",
                     "C1_growth_spillover", "C2_welfare_distribution", "C3_external_balance", "C4_price_stability"]
        eipi = eipi[[c for c in eipi_cols if c in eipi.columns]]

        network = self._read_sheet(self.network_path, ["coefficient_based_indicators", "network_results"])
        network = network[[c for c in RAW_NETWORK_INDICATORS if c in network.columns]]

        cge = self._read_sheet(self.cge_path, ["counterfactual_10pct", "uniform_10pct"])
        cge_keep = CGE_OUTCOMES + [c.replace("_pct_change", "_base") for c in CGE_OUTCOMES]
        cge = cge[[c for c in cge_keep if c in cge.columns]]

        io = pd.read_excel(self.io_path, header=0, index_col=0)
        total_output = io.iloc[:, -1].astype(float)
        total_output.name = "total_output"
        domar_weight = total_output / total_output.sum()
        domar_weight.name = "domar_weight"
        log_output = np.log(total_output)
        log_output.name = "log_output"
        frames = [snii, eipi, network, cge, total_output, domar_weight, log_output]
        common_index = frames[0].index
        for f in frames[1:]:
            common_index = common_index.intersection(f.index)
        common_index = sorted(common_index, key=lambda s: int(str(s).replace("sec", "")))

        assert len(common_index) == 62, (
            f"Expected 62 aligned sectors after merging all inputs, found {len(common_index)}. "
            f"Check that all files share the same 'secN' sector labels."
        )
        for name, f in zip(
            ["SNII", "EIPI", "network", "CGE", "total_output", "domar_weight", "log_output"], frames
        ):
            missing = set(common_index) - set(f.index)
            assert not missing, f"{name} is missing sectors: {sorted(missing)}"

        panel = pd.concat([f.reindex(common_index) for f in frames], axis=1)
        panel.index.name = "sector"

        self.panel = panel
        self.sectors = list(common_index)
        return panel

    # ------------------------------------------------------------------
    # Section 3: strategic classification
    # ------------------------------------------------------------------
    def _quadrant_labels(self, vertical: pd.Series, horizontal: pd.Series, v_thresh, h_thresh) -> pd.Series:
        labels = {}
        for sector in vertical.index:
            v_high = vertical[sector] >= v_thresh
            h_high = horizontal[sector] >= h_thresh
            labels[sector] = CATEGORY_LABELS[(v_high, h_high)]
        return pd.Series(labels, name="category")

    def classify(self) -> pd.DataFrame:
        """Build the main (headline) classification -- SNII_DIM (vertical) x
        EIPI_CH_size_controlled (horizontal), mean threshold -- plus the
        four robustness variants (Section 3.2), the median-threshold variant
        (Section 3.3), and a borderline-sector flag."""
        if self.panel is None:
            self.build_panel()
        p = self.panel

        v, h = p[self.snii_axis], p[self.eipi_axis]
        v_mean, h_mean = v.mean(), h.mean()
        v_median, h_median = v.median(), h.median()

        table = pd.DataFrame(index=p.index)
        table["category_main_mean"] = self._quadrant_labels(v, h, v_mean, h_mean)
        table["category_main_median"] = self._quadrant_labels(v, h, v_median, h_median)

        variant_axes = {
            "variant_snii_eipi_ch": ("SNII_DIM", "EIPI_CH"),
            "variant_topsis": ("SNII_TOPSIS", "EIPI_TOPSIS_size_controlled"),
            "variant_pca": ("SNII_PCA", "EIPI_PCA_size_controlled"),
            "variant_avg": ("SNII_AVG", "EIPI_AVG_size_controlled"),
        }
        for col_name, (v_col, h_col) in variant_axes.items():
            vv, hh = p[v_col], p[h_col]
            table[f"category_{col_name}"] = self._quadrant_labels(vv, hh, vv.mean(), hh.mean())

        # borderline flag (Section 3.3): within +/- BORDERLINE_BAND_FRACTION
        # of each axis's range around the mean threshold
        v_band = self.BORDERLINE_BAND_FRACTION * (v.max() - v.min())
        h_band = self.BORDERLINE_BAND_FRACTION * (h.max() - h.min())
        table["borderline"] = ((v - v_mean).abs() <= v_band) | ((h - h_mean).abs() <= h_band)

        n_changed_mean_vs_median = int((table["category_main_mean"] != table["category_main_median"]).sum())

        self.classifications = table
        self.classification_meta = {
            "v_mean": v_mean, "h_mean": h_mean, "v_median": v_median, "h_median": h_median,
            "v_band": v_band, "h_band": h_band,
            "n_changed_mean_vs_median": n_changed_mean_vs_median,
            "variant_axes": variant_axes,
        }
        return table

    # ------------------------------------------------------------------
    # Section 4, H1
    # ------------------------------------------------------------------
    def test_h1(self, eipi_target=None) -> dict:
        """(a) SNII_DIM vs primary EIPI target; (b) each of the 7 raw
        network indicators vs the primary EIPI target; (c) each network
        indicator vs each of the 7 CGE outcomes. All bivariate, Spearman +
        Pearson, with p-values (Section 4, H1)."""
        if self.panel is None:
            self.build_panel()
        p = self.panel
        target = eipi_target or self.eipi_axis

        snii_eipi_spearman = spearmanr(p[self.snii_axis], p[target])
        snii_eipi_pearson = pearsonr(p[self.snii_axis], p[target])
        headline = pd.Series({
            "spearman_rho": snii_eipi_spearman.statistic, "spearman_p": snii_eipi_spearman.pvalue,
            "pearson_r": snii_eipi_pearson[0], "pearson_p": snii_eipi_pearson[1],
        }, name=f"{self.snii_axis}_vs_{target}")

        rows = []
        for ind in RAW_NETWORK_INDICATORS:
            sr = spearmanr(p[ind], p[target])
            pr = pearsonr(p[ind], p[target])
            rows.append({"indicator": ind, "spearman_rho": sr.statistic, "spearman_p": sr.pvalue,
                         "pearson_r": pr[0], "pearson_p": pr[1]})
        metric_vs_eipi = pd.DataFrame(rows).set_index("indicator")

        rho_matrix = pd.DataFrame(index=RAW_NETWORK_INDICATORS, columns=CGE_OUTCOMES, dtype=float)
        p_matrix = pd.DataFrame(index=RAW_NETWORK_INDICATORS, columns=CGE_OUTCOMES, dtype=float)
        for ind in RAW_NETWORK_INDICATORS:
            for outcome in CGE_OUTCOMES:
                sr = spearmanr(p[ind], p[outcome])
                rho_matrix.loc[ind, outcome] = sr.statistic
                p_matrix.loc[ind, outcome] = sr.pvalue

        return {
            "headline": headline,
            "metric_vs_eipi": metric_vs_eipi,
            "metric_vs_outcome_rho": rho_matrix,
            "metric_vs_outcome_p": p_matrix,
        }

    # ------------------------------------------------------------------
    # H2
    # ------------------------------------------------------------------
    def test_h2(self, top_n=8) -> dict:
        """Quantify divergence between structural centrality and GE
        importance: off-diagonal sector count in the main classification,
        and the most divergent sectors in each direction, with their raw
        CGE indicators as a candidate economic mechanism (Section 4, H2)."""
        if self.classifications is None:
            self.classify()
        p, table = self.panel, self.classifications

        off_diagonal_categories = ["Emerging Leverage Sectors", "Structurally Critical but Low-Transmission Sectors"]
        off_diagonal = table["category_main_mean"].isin(off_diagonal_categories)
        n_off_diagonal = int(off_diagonal.sum())

        snii_pct = p[self.snii_axis].rank(pct=True)
        eipi_pct = p[self.eipi_axis].rank(pct=True)
        divergence = (snii_pct - eipi_pct)  # positive: high SNII / low EIPI; negative: reverse

        high_snii_low_eipi = divergence.sort_values(ascending=False).head(top_n).index
        low_snii_high_eipi = divergence.sort_values(ascending=True).head(top_n).index

        detail_cols = [self.snii_axis, self.eipi_axis] + CGE_OUTCOMES
        high_snii_low_eipi_detail = p.loc[high_snii_low_eipi, detail_cols].copy()
        high_snii_low_eipi_detail["divergence_percentile_gap"] = divergence.loc[high_snii_low_eipi]

        low_snii_high_eipi_detail = p.loc[low_snii_high_eipi, detail_cols].copy()
        low_snii_high_eipi_detail["divergence_percentile_gap"] = divergence.loc[low_snii_high_eipi]

        return {
            "n_off_diagonal": n_off_diagonal,
            "n_total": len(table),
            "off_diagonal_share": n_off_diagonal / len(table),
            "high_snii_low_eipi": high_snii_low_eipi_detail,
            "low_snii_high_eipi": low_snii_high_eipi_detail,
        }

    # ------------------------------------------------------------------
    # shared LOOCV machinery for H3 / H4 / robustness
    # ------------------------------------------------------------------
    def _make_model(self, model_type: str):
        if model_type == "elasticnet":
            # A trimmed l1_ratio/alpha grid (vs. sklearn's dense default) keeps
            # the nested LOOCV-within-5-fold search tractable at n=62 while
            # still spanning ridge-like (l1_ratio~0.3) to lasso-like (l1_ratio=1) regularization.
            estimator = ElasticNetCV(l1_ratio=[.3, .6, .9, 1.0], alphas=50, cv=5,
                                      random_state=self.RANDOM_STATE, max_iter=10000)
        elif model_type == "ridge":
            estimator = RidgeCV(alphas=np.logspace(-3, 3, 25))
        elif model_type == "ols":
            estimator = LinearRegression()
        else:
            raise ValueError(f"Unknown model_type: {model_type}")
        return Pipeline([("scaler", StandardScaler()), ("model", estimator)])

    def _loocv_metrics(self, X: pd.DataFrame, y: pd.Series, model_type="elasticnet",
                        degenerate_rel_tol=0.03) -> dict:
        pipeline = self._make_model(model_type)
        y_pred = cross_val_predict(pipeline, X.values, y.values, cv=LeaveOneOut())
        r2 = r2_score(y.values, y_pred)

        y_std = float(np.std(y.values))
        pred_std = float(np.std(y_pred))
        degenerate = pred_std < max(degenerate_rel_tol * y_std, 1e-10)

        if degenerate:
            rho, p_val = np.nan, np.nan
        else:
            res = spearmanr(y.values, y_pred)
            rho, p_val = res.statistic, res.pvalue

        return {"loocv_r2": r2, "spearman_rho": rho, "spearman_p": p_val,
                "degenerate": degenerate, "n_predictors": X.shape[1], "n_obs": len(y)}

    # ------------------------------------------------------------------
    # Section 4, H3
    # ------------------------------------------------------------------
    def test_h3(self, model_type="elasticnet") -> pd.DataFrame:
        """For each of the seven CGE pct_change outcomes, predict it
        out-of-sample (LOOCV) from each predictor set (Section 2.1): the
        four SNII dimension sub-scores (primary), the retained-5 selected
        indicators, all-7 raw indicators, and SNII_DIM alone (robustness).
        Degenerate (near-constant) predictions are flagged, not reported as
        a spurious rank correlation."""
        if self.panel is None:
            self.build_panel()
        p = self.panel

        rows = []
        for set_name, cols in PREDICTOR_SETS.items():
            X = p[cols]
            for outcome in CGE_OUTCOMES:
                y = p[outcome]
                metrics = self._loocv_metrics(X, y, model_type=model_type)
                rows.append({"predictor_set": set_name, "outcome": outcome, **metrics})

        return pd.DataFrame(rows).set_index(["predictor_set", "outcome"])

    # ------------------------------------------------------------------
    # classification agreement
    # ------------------------------------------------------------------
    def classification_agreement(self) -> dict:
        """Cross-tabulate SNII-only (central/peripheral by SNII_DIM mean)
        against EIPI-only (by EIPI target mean) classification: confusion
        matrix + Cohen's kappa."""
        if self.panel is None:
            self.build_panel()
        p = self.panel

        snii_central = p[self.snii_axis] >= p[self.snii_axis].mean()
        eipi_central = p[self.eipi_axis] >= p[self.eipi_axis].mean()

        snii_labels = snii_central.map({True: "SNII: central", False: "SNII: peripheral"})
        eipi_labels = eipi_central.map({True: "EIPI: high-impact", False: "EIPI: low-impact"})

        confusion = pd.crosstab(snii_labels, eipi_labels)
        kappa = cohen_kappa_score(snii_central.values, eipi_central.values)

        return {"confusion_matrix": confusion, "cohen_kappa": kappa}

    # ------------------------------------------------------------------
    # Section 5: robustness
    # ------------------------------------------------------------------
    def _variant_stability_kappa(self) -> pd.DataFrame:
        if self.classifications is None:
            self.classify()
        variant_cols = ["category_main_mean", "category_variant_snii_eipi_ch", "category_variant_topsis",
                          "category_variant_pca", "category_variant_avg"]
        table = self.classifications[variant_cols]
        kappa_matrix = pd.DataFrame(index=variant_cols, columns=variant_cols, dtype=float)
        agreement_matrix = pd.DataFrame(index=variant_cols, columns=variant_cols, dtype=float)
        for a in variant_cols:
            for b in variant_cols:
                kappa_matrix.loc[a, b] = cohen_kappa_score(table[a], table[b])
                agreement_matrix.loc[a, b] = float((table[a] == table[b]).mean())
        return kappa_matrix, agreement_matrix

    def robustness(self) -> dict:
        """Section 5 robustness bundle: (1) threshold mean-vs-median,
        (2) variant-classification stability (kappa), (3) ground-truth
        (3) predictive model comparison (elastic net vs ridge vs OLS),
        (5) predictor-set comparison (already embedded in H3/H4 output).

        """
        if self.classifications is None:
            self.classify()

        kappa_matrix, agreement_matrix = self._variant_stability_kappa()

        h1_primary = self.test_h1(eipi_target="EIPI_CH")

        # (4) model comparison on H3 (dimension sub-scores predictor set only, for tractability)
        model_comparison_rows = []
        p = self.panel
        X_primary = p[PREDICTOR_SETS["dimension_subscores"]]
        for model_type in ["elasticnet", "ridge", "ols"]:
            for outcome in CGE_OUTCOMES:
                metrics = self._loocv_metrics(X_primary, p[outcome], model_type=model_type)
                model_comparison_rows.append({"model": model_type, "outcome": outcome, **metrics})
        model_comparison = pd.DataFrame(model_comparison_rows).set_index(["model", "outcome"])

        return {
            "threshold_n_changed_mean_vs_median": self.classification_meta["n_changed_mean_vs_median"],
            "variant_stability_kappa": kappa_matrix,
            "variant_stability_agreement_pct": agreement_matrix,
            "borderline_sectors": self.classifications.index[self.classifications["borderline"]].tolist(),
            "ground_truth_headline": h1_primary["headline"],
            "model_comparison": model_comparison,
        }
