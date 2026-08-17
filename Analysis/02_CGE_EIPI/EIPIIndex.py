import numpy as np
import pandas as pd
from scipy import stats
from sklearn.decomposition import PCA
from statsmodels.stats.outliers_influence import variance_inflation_factor
from statsmodels.tools.tools import add_constant
import statsmodels.api as sm

try:
    from factor_analyzer.factor_analyzer import calculate_kmo, calculate_bartlett_sphericity
    _HAS_FACTOR_ANALYZER = True
except ImportError:  # pragma: no cover
    _HAS_FACTOR_ANALYZER = False


CHANNELS = {
    "C1_growth_spillover": {"benefit": ["GDP", "Spillover"], "cost": []},
    "C2_welfare_distribution": {"benefit": ["Welfare", "LaborIncomeShare"], "cost": []},
    "C3_external_balance": {"benefit": ["ExportShare"], "cost": ["ImportDependency"]},
    "C4_price_stability": {"benefit": [], "cost": ["CPI"]},
}
BENEFIT_COLS = ["GDP", "Welfare", "ExportShare", "LaborIncomeShare", "Spillover"]
COST_COLS = ["ImportDependency", "CPI"]
ALL_INDICATORS = BENEFIT_COLS + COST_COLS


class EIPIComposite:
    """
    Economy-wide Impact Performance Index (EIPI) construction. Mirrors the
    SNII_II methodology (channel/dimension-balanced primary index, entropy
    and equal-weight TOPSIS + PCA as comparisons, size control, transparent
    diagnostics) so SNII and EIPI can later be combined for hypothesis
    testing. Operates only on the seven `<indicator>_pct_change` columns
    produced by CGEModel.counterfactual_result().
    """

    RANDOM_STATE = 42
    REDUNDANCY_FLAG_THRESHOLD = 0.95

    def __init__(self, cge_results: pd.DataFrame, sector_output: pd.Series):
        pct_change_cols = {f"{c}_pct_change": c for c in ALL_INDICATORS}
        missing = [c for c in pct_change_cols if c not in cge_results.columns]
        if missing:
            raise KeyError(f"cge_results is missing required columns: {missing}")
        self.raw = cge_results[list(pct_change_cols)].rename(columns=pct_change_cols).astype(float)
        self.sector_output = sector_output.reindex(self.raw.index).astype(float)

        self.selection_ = None
        self.results_ = None
        self.meta_ = None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _minmax(s: pd.Series) -> pd.Series:
        lo, hi = s.min(), s.max()
        if np.isclose(hi, lo):
            # Edge case: constant criterion -> neutral contribution.
            return pd.Series(0.5, index=s.index)
        return (s - lo) / (hi - lo)

    @staticmethod
    def _zscore(df: pd.DataFrame) -> pd.DataFrame:
        std = df.std(ddof=0)
        std_safe = std.replace(0, 1.0)
        return (df - df.mean()) / std_safe

    def _oriented_raw(self, indicator: str) -> pd.Series:
        """Sign-flip cost indicators so that, from here on, higher == better
        for every indicator (Section 1.7 / 4.1)."""
        s = self.raw[indicator]
        return -s if indicator in COST_COLS else s

    @classmethod
    def _entropy_weights(cls, oriented_df: pd.DataFrame) -> pd.Series:
        """Shannon-entropy weighting on already benefit-oriented data. Entropy
        requires non-negative proportions; oriented pct-change data is
        frequently negative, so each column is shifted by its own minimum
        (Section 1.8 mandatory non-negativity shift) before computing shares."""
        X = oriented_df - oriented_df.min()
        # handle exact zero case
        for c in X.columns:
            if (X[c] < 0).any():
                X[c] = X[c] - X[c].min()
        m = X.shape[0]
        col_sums = X.sum(axis=0).replace(0, np.nan)
        P = X.div(col_sums, axis=1)
        k = 1.0 / np.log(m)
        with np.errstate(divide="ignore", invalid="ignore"):
            plogp = (P * np.log(P)).fillna(0.0)
        e = (-k * plogp.sum(axis=0)).fillna(1.0)
        d = 1.0 - e
        if np.isclose(d.sum(), 0.0):
            return pd.Series(1.0 / len(d), index=d.index)
        return d / d.sum()

    @classmethod
    def _topsis(cls, oriented_df: pd.DataFrame, weights: pd.Series = None):
        """TOPSIS on already benefit-oriented (cost-flipped) data: positive
        ideal = max, negative ideal = min for every column -- mathematically
        equivalent to native cost-min-ideal TOPSIS on the un-flipped data
        (vector normalization is odd/sign-symmetric), while letting the rest
        of the pipeline treat every column uniformly. Returns closeness in
        [0, 1]."""
        if weights is None:
            weights = cls._entropy_weights(oriented_df)
        weights = weights.reindex(oriented_df.columns)

        denom = np.sqrt((oriented_df ** 2).sum(axis=0)).replace(0, 1.0)
        norm = oriented_df.div(denom, axis=1)
        weighted = norm.mul(weights, axis=1)

        ideal_best = weighted.max(axis=0)
        ideal_worst = weighted.min(axis=0)
        d_best = np.sqrt(((weighted - ideal_best) ** 2).sum(axis=1))
        d_worst = np.sqrt(((weighted - ideal_worst) ** 2).sum(axis=1))
        denom_c = (d_best + d_worst).replace(0, np.nan)
        closeness = (d_worst / denom_c).fillna(0.5)
        return closeness, weights

    @staticmethod
    def _channel_of(indicator: str) -> str:
        for ch, spec in CHANNELS.items():
            if indicator in spec["benefit"] or indicator in spec["cost"]:
                return ch
        raise KeyError(indicator)

    @staticmethod
    def _compute_vif(df: pd.DataFrame) -> pd.Series:
        if df.shape[1] < 2:
            return pd.Series(1.0, index=df.columns)
        std = df.std(ddof=0).replace(0, 1.0)
        z = (df - df.mean()) / std
        X = add_constant(z, has_constant="add")
        vifs = {}
        for col in df.columns:
            idx = list(X.columns).index(col)
            try:
                vifs[col] = variance_inflation_factor(X.values, idx)
            except Exception:
                vifs[col] = np.inf
        return pd.Series(vifs)

    @staticmethod
    def _kmo_bartlett(df: pd.DataFrame):
        if not _HAS_FACTOR_ANALYZER or df.shape[1] < 3:
            return None, None, None, None
        try:
            kmo_per_item, kmo_overall = calculate_kmo(df.values)
            chi2, p = calculate_bartlett_sphericity(df.values)
            return kmo_overall, pd.Series(kmo_per_item, index=df.columns), chi2, p
        except Exception:
            return None, None, None, None

    # ------------------------------------------------------------------
    # Section 4.4: redundancy / diagnostics (report only -- no dropping)
    # ------------------------------------------------------------------
    def select_indicators(self) -> dict:
        spearman = self.raw.corr(method="spearman")
        vif = self._compute_vif(self.raw)
        kmo_overall, kmo_per_item, bartlett_chi2, bartlett_p = self._kmo_bartlett(self.raw)

        flagged_pairs = []
        cols = list(self.raw.columns)
        for a_idx in range(len(cols)):
            for b_idx in range(a_idx + 1, len(cols)):
                a, b = cols[a_idx], cols[b_idx]
                rho = spearman.loc[a, b]
                if abs(rho) >= self.REDUNDANCY_FLAG_THRESHOLD:
                    flagged_pairs.append((a, b, float(rho)))

        # Anti-degeneracy check (Section 1.2): the functional labor-share
        # indicator must NOT be (near-)collinear with GDP -- if it were, the
        # numeraire/definition choice would be wrong.
        labor_gdp_corr = float(self.raw["LaborIncomeShare"].corr(self.raw["GDP"], method="pearson"))
        labor_degenerate = abs(labor_gdp_corr) >= 0.99
        assert not labor_degenerate, (
            f"Anti-degeneracy check FAILED: |corr(LaborIncomeShare_pct_change, GDP_pct_change)| "
            f"= {abs(labor_gdp_corr):.4f} >= 0.99. Numeraire/labor-indicator definition must be revisited."
        )

        self.selection_ = {
            "spearman": spearman,
            "vif": vif,
            "flagged_pairs": flagged_pairs,
            "kmo_overall": kmo_overall,
            "kmo_per_item": kmo_per_item,
            "bartlett_chi2": bartlett_chi2,
            "bartlett_p": bartlett_p,
            "labor_gdp_correlation": labor_gdp_corr,
            "labor_degenerate": labor_degenerate,
        }
        return self.selection_

    # ------------------------------------------------------------------
    # Section 4.1: channel-balanced index (PRIMARY)
    # ------------------------------------------------------------------
    def _channel_subscores(self):
        scores = {}
        weights_report = {}
        for ch, spec in CHANNELS.items():
            members = spec["benefit"] + spec["cost"]
            if len(members) == 1:
                oriented = self._oriented_raw(members[0])
                scores[ch] = self._minmax(oriented)
                weights_report[ch] = {members[0]: 1.0}
            else:
                oriented_df = pd.DataFrame({m: self._oriented_raw(m) for m in members})
                sub, w = self._topsis(oriented_df)
                scores[ch] = sub
                weights_report[ch] = w.to_dict()
        return pd.DataFrame(scores), weights_report

    def _eipi_ch(self):
        sub_df, weights_report = self._channel_subscores()
        n_ch = sub_df.shape[1]
        equal_w = 1.0 / n_ch
        eipi_ch = (sub_df * equal_w).sum(axis=1)
        return eipi_ch, sub_df, weights_report, equal_w

    # ------------------------------------------------------------------
    # Section 4.2: entropy-TOPSIS / equal-weight TOPSIS / PCA (comparison)
    # ------------------------------------------------------------------
    def _oriented_all(self):
        return pd.DataFrame({c: self._oriented_raw(c) for c in ALL_INDICATORS})

    def _eipi_topsis(self):
        oriented = self._oriented_all()
        closeness_entropy, entropy_w = self._topsis(oriented)
        equal_weights = pd.Series(1.0 / len(ALL_INDICATORS), index=ALL_INDICATORS)
        closeness_equal, _ = self._topsis(oriented, weights=equal_weights)
        return closeness_entropy, entropy_w, closeness_equal

    def _eipi_pca(self):
        oriented = self._oriented_all()
        Z = self._zscore(oriented)
        n_comp = min(Z.shape[0], Z.shape[1])
        pca = PCA(n_components=n_comp, random_state=self.RANDOM_STATE)
        scores = pca.fit_transform(Z.values)
        evr = pca.explained_variance_ratio_
        eigenvalues = pca.explained_variance_

        if evr[0] >= 0.60:
            comps_used = 1
        else:
            kaiser = np.where(eigenvalues > 1)[0]
            cum = np.cumsum(evr)
            cum80 = np.where(cum >= 0.80)[0]
            n_kaiser = (kaiser[-1] + 1) if len(kaiser) else 1
            n_cum80 = (cum80[0] + 1) if len(cum80) else len(evr)
            comps_used = max(1, min(n_kaiser, n_cum80))

        loadings = pd.DataFrame(
            pca.components_[:comps_used].T, index=ALL_INDICATORS,
            columns=[f"PC{i+1}" for i in range(comps_used)],
        )

        oriented_scores = scores[:, :comps_used].copy()
        mean_indicator = oriented.mean(axis=1).values
        for k in range(comps_used):
            corr_k = np.corrcoef(oriented_scores[:, k], mean_indicator)[0, 1]
            if corr_k < 0:
                oriented_scores[:, k] *= -1
                loadings.iloc[:, k] *= -1

        w = evr[:comps_used] / evr[:comps_used].sum()
        combined = (oriented_scores * w).sum(axis=1)
        eipi_pca = self._minmax(pd.Series(combined, index=oriented.index))
        return eipi_pca, evr, loadings, comps_used, eigenvalues

    # ------------------------------------------------------------------
    # discrimination diagnostics
    # ------------------------------------------------------------------
    @staticmethod
    def _gini(s: pd.Series) -> float:
        x = np.sort(s.values.astype(float))
        x = x - x.min() + 1e-12
        n = len(x)
        cum = np.cumsum(x)
        return (n + 1 - 2 * (cum.sum() / cum[-1])) / n if cum[-1] != 0 else 0.0

    @classmethod
    def _distribution_stats(cls, s: pd.Series) -> dict:
        return {
            "skew": float(stats.skew(s.values)),
            "gini": float(cls._gini(s)),
            "top10_share": float(s.sort_values(ascending=False).head(10).sum() / s.sum()) if s.sum() != 0 else np.nan,
        }

    # ------------------------------------------------------------------
    # Section 4.3: composite and outputs
    # ------------------------------------------------------------------
    def general_results(self) -> pd.DataFrame:
        eipi_ch_raw, sub_df, ch_weights, equal_w = self._eipi_ch()
        # Fix: min-max normalize primary EIPI_CH index to [0, 1] range
        eipi_ch = self._minmax(eipi_ch_raw)

        closeness_entropy, entropy_w, closeness_equal = self._eipi_topsis()
        eipi_pca, evr, loadings, comps_used, eigenvalues = self._eipi_pca()

        combo = pd.concat([
            self._zscore(pd.DataFrame({"EIPI_CH": eipi_ch})),
            self._zscore(pd.DataFrame({"EIPI_TOPSIS": closeness_entropy})),
            self._zscore(pd.DataFrame({"EIPI_PCA": eipi_pca})),
        ], axis=1)
        eipi_avg = self._minmax(combo.mean(axis=1))

        out = pd.DataFrame({
            "EIPI_CH": eipi_ch,
            "EIPI_TOPSIS": closeness_entropy,
            "EIPI_EQUAL_TOPSIS": closeness_equal,
            "EIPI_PCA": eipi_pca,
            "EIPI_AVG": eipi_avg,
        })
        for c in sub_df.columns:
            out[c] = sub_df[c]

        for col in ["EIPI_CH", "EIPI_TOPSIS", "EIPI_EQUAL_TOPSIS", "EIPI_PCA", "EIPI_AVG"]:
            out[f"rank_{col}"] = out[col].rank(ascending=False, method="min").astype(int)
            out[f"{col}_pctrank"] = out[col].rank(pct=True)

        log_output = np.log(self.sector_output.reindex(out.index).clip(lower=1e-6))
        X = sm.add_constant(log_output)
        size_control_models = {}
        for col in ["EIPI_CH", "EIPI_TOPSIS", "EIPI_PCA", "EIPI_AVG"]:
            model = sm.OLS(out[col].values, X.values).fit()
            controlled = f"{col}_size_controlled"
            out[controlled] = self._minmax(pd.Series(model.resid, index=out.index))
            out[f"rank_{controlled}"] = out[controlled].rank(ascending=False, method="min").astype(int)
            size_control_models[col] = model

        self.results_ = out
        self.meta_ = {
            "channel_weights": ch_weights,
            "equal_channel_weight": equal_w,
            "entropy_weights": entropy_w.to_dict(),
            "pca_explained_variance_ratio": evr,
            "pca_eigenvalues": eigenvalues,
            "pca_loadings": loadings,
            "pca_components_used": comps_used,
            "size_control_ols_r2": {col: model.rsquared for col, model in size_control_models.items()},
            "distribution": {c: self._distribution_stats(out[c]) for c in
                              ["EIPI_CH", "EIPI_TOPSIS", "EIPI_EQUAL_TOPSIS", "EIPI_PCA", "EIPI_AVG"]},
        }
        return out

    def size_diagnostic(self) -> dict:
        if self.results_ is None:
            self.general_results()
        output_share = self.sector_output.reindex(self.results_.index) / self.sector_output.sum()
        rows = []
        for col in ["EIPI_CH", "EIPI_TOPSIS", "EIPI_EQUAL_TOPSIS", "EIPI_PCA", "EIPI_AVG"]:
            s = self.results_[col]
            rows.append({"indicator": col, "spearman_vs_output_share": s.corr(output_share, method="spearman"),
                         "pearson_vs_output_share": s.corr(output_share, method="pearson")})
        size_corr = pd.DataFrame(rows).set_index("indicator")
        before = self.results_["EIPI_CH"].nlargest(10).index.tolist()
        after = self.results_["EIPI_CH_size_controlled"].nlargest(10).index.tolist()
        return {"output_share": output_share, "size_correlations": size_corr,
                "top10_before_size_control": before, "top10_after_size_control": after,
                "top10_overlap_count": len(set(before) & set(after))}

    # ------------------------------------------------------------------
    # Section 7: robustness
    # ------------------------------------------------------------------
    def robustness_report(self) -> dict:
        if self.results_ is None:
            self.general_results()
        res = self.results_

        model_cols = ["EIPI_CH", "EIPI_TOPSIS", "EIPI_EQUAL_TOPSIS", "EIPI_PCA"]
        model_corr = res[model_cols].corr(method="spearman")

        minmax_vs_rank_corr = {}
        for col in ["EIPI_CH", "EIPI_TOPSIS", "EIPI_PCA", "EIPI_AVG"]:
            minmax_vs_rank_corr[col] = res[col].corr(res[f"{col}_pctrank"], method="spearman")

        size_diag = self.size_diagnostic()
        top10_sets = {name: set(res[name].sort_values(ascending=False).head(10).index) for name in model_cols}
        for name in ["EIPI_CH_size_controlled", "EIPI_TOPSIS_size_controlled",
                     "EIPI_PCA_size_controlled", "EIPI_AVG_size_controlled"]:
            top10_sets[name] = set(res[name].nlargest(10).index)
        common_all = set.intersection(*top10_sets.values())

        return {
            "model_rank_correlation": model_corr,
            "minmax_vs_rank_normalization_correlation": minmax_vs_rank_corr,
            "size_vs_size_controlled_top10_overlap": size_diag["top10_overlap_count"],
            "top10_sets": top10_sets,
            "top10_common_across_all_variants": common_all,
        }
