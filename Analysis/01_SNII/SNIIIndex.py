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


DIMENSIONS = {
    "D1_direct": ["backward_linkage", "forward_linkage"],
    "D2_embeddedness": ["eigenvector_centrality", "pagerank"],
    "D3_bridge": ["betweenness_centrality"],
    "D4_supply": ["hub_score", "authority_score"],
}
ALL_INDICATORS = [c for cols in DIMENSIONS.values() for c in cols]


class SNIIComposite:
    """
    Structural Network Importance Index (SNII) construction.

    Pipeline: redundancy screen (|Spearman| >= 0.85) + VIF pruning (< 5)
    -> dimension-balanced index (PRIMARY) + entropy-TOPSIS / equal-weight
    TOPSIS / PCA as comparisons -> size diagnostic -> robustness checks.
    """

    RANDOM_STATE = 42

    def __init__(self, network_results: pd.DataFrame, total_output: pd.Series,
                 redundancy_threshold: float = 0.85, vif_threshold: float = 5.0):
        self.raw = network_results[ALL_INDICATORS].copy()
        self.total_output = total_output.reindex(self.raw.index).astype(float)
        self.redundancy_threshold = redundancy_threshold
        self.vif_threshold = vif_threshold

        self.selection_ = None
        self.retained_ = None
        self.dropped_ = None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _minmax(s: pd.Series) -> pd.Series:
        lo, hi = s.min(), s.max()
        if np.isclose(hi, lo):
            return pd.Series(0.5, index=s.index)
        return (s - lo) / (hi - lo)

    @staticmethod
    def _zscore(df: pd.DataFrame) -> pd.DataFrame:
        std = df.std(ddof=0)
        std_safe = std.replace(0, 1.0)
        return (df - df.mean()) / std_safe

    @staticmethod
    def _entropy_weights(df: pd.DataFrame) -> pd.Series:
        """Standard Shannon-entropy weighting. Requires non-negative data;
        indicators are already min-max scaled to [0,1] upstream, so a small
        shift handles the (rare) exact-zero-column edge case only."""
        X = df.copy()
        # non-negativity shift (documented edge case)
        for c in X.columns:
            if (X[c] < 0).any():
                X[c] = X[c] - X[c].min()
        m = X.shape[0]
        col_sums = X.sum(axis=0)
        col_sums_safe = col_sums.replace(0, np.nan)
        P = X.div(col_sums_safe, axis=1)
        k = 1.0 / np.log(m)
        with np.errstate(divide="ignore", invalid="ignore"):
            plogp = P * np.log(P)
        plogp = plogp.fillna(0.0)
        e = -k * plogp.sum(axis=0)
        e = e.fillna(1.0)  # constant column -> zero information -> entropy 1 -> weight 0
        d = 1.0 - e
        if np.isclose(d.sum(), 0.0):
            # all indicators equally uninformative -> fall back to equal weights
            return pd.Series(1.0 / len(d), index=d.index)
        w = d / d.sum()
        return w

    @classmethod
    def _topsis(cls, df: pd.DataFrame, weights: pd.Series = None):
        """Entropy (or supplied) weighted TOPSIS on benefit-only indicators
        already scaled to [0,1]. Returns (closeness Series in [0,1], weights used)."""
        if weights is None:
            weights = cls._entropy_weights(df)
        weights = weights.reindex(df.columns)

        denom = np.sqrt((df ** 2).sum(axis=0))
        denom_safe = denom.replace(0, 1.0)
        norm = df.div(denom_safe, axis=1)
        weighted = norm.mul(weights, axis=1)

        ideal_best = weighted.max(axis=0)
        ideal_worst = weighted.min(axis=0)

        d_best = np.sqrt(((weighted - ideal_best) ** 2).sum(axis=1))
        d_worst = np.sqrt(((weighted - ideal_worst) ** 2).sum(axis=1))

        denom_c = (d_best + d_worst).replace(0, np.nan)
        closeness = (d_worst / denom_c).fillna(0.5)
        return closeness, weights

    @staticmethod
    def _dimension_of(indicator: str) -> str:
        for dim, cols in DIMENSIONS.items():
            if indicator in cols:
                return dim
        raise KeyError(indicator)

    @staticmethod
    def _compute_vif(df: pd.DataFrame) -> pd.Series:
        if df.shape[1] < 2:
            return pd.Series(1.0, index=df.columns)
        X = add_constant(cls_safe_std(df), has_constant="add")
        vifs = {}
        for i, col in enumerate(df.columns):
            idx = list(X.columns).index(col)
            try:
                vifs[col] = variance_inflation_factor(X.values, idx)
            except Exception:
                vifs[col] = np.inf
        return pd.Series(vifs)

    # ------------------------------------------------------------------
    # Section 4: Input selection
    # ------------------------------------------------------------------
    def select_indicators(self) -> dict:
        candidates = self.raw.copy()
        spearman_before = candidates.corr(method="spearman")
        vif_before = self._compute_vif(candidates)
        entropy_full = self._entropy_weights(candidates)

        retained = list(candidates.columns)
        reasons = {}
        log = []

        max_iter = 20
        for _ in range(max_iter):
            sub = candidates[retained]
            corr = sub.corr(method="spearman").abs()
            corr_arr = corr.to_numpy(copy=True)
            np.fill_diagonal(corr_arr, 0.0)
            corr = pd.DataFrame(corr_arr, index=corr.index, columns=corr.columns)
            worst_val = corr.values.max()
            if worst_val < self.redundancy_threshold or len(retained) <= 4:
                vif_now = self._compute_vif(sub)
                if worst_val < self.redundancy_threshold and (vif_now < self.vif_threshold).all():
                    break

            # locate worst offending pair
            idx = np.unravel_index(np.argmax(corr.values), corr.values.shape)
            a, b = corr.index[idx[0]], corr.columns[idx[1]]
            if corr.loc[a, b] < self.redundancy_threshold:
                # redundancy resolved; check VIF-only violations
                vif_now = self._compute_vif(sub)
                offenders = vif_now[vif_now >= self.vif_threshold]
                if offenders.empty:
                    break
                # drop highest-VIF indicator not the last of its dimension
                offenders_sorted = offenders.sort_values(ascending=False)
                dropped_any = False
                for cand in offenders_sorted.index:
                    dim = self._dimension_of(cand)
                    dim_members_left = [c for c in retained if self._dimension_of(c) == dim]
                    if len(dim_members_left) > 1:
                        retained.remove(cand)
                        reasons[cand] = (
                            f"dropped: VIF={vif_now[cand]:.2f} >= {self.vif_threshold} "
                            f"(dimension {dim} retains other member(s))"
                        )
                        log.append(f"Removed {cand} (high VIF {vif_now[cand]:.2f})")
                        dropped_any = True
                        break
                if not dropped_any:
                    log.append("VIF violation remains but cannot drop last-of-dimension indicators; stopping.")
                    break
                continue

            vif_now = self._compute_vif(sub)
            dim_a, dim_b = self._dimension_of(a), self._dimension_of(b)
            members_a = [c for c in retained if self._dimension_of(c) == dim_a]
            members_b = [c for c in retained if self._dimension_of(c) == dim_b]

            can_drop_a = len(members_a) > 1
            can_drop_b = len(members_b) > 1

            if not can_drop_a and not can_drop_b:
                log.append(
                    f"Pair ({a},{b}) exceeds threshold ({corr.loc[a,b]:.2f}) but both are last-of-dimension; retaining both."
                )
                # avoid infinite loop: mask this pair by nudging threshold check via break
                break

            if can_drop_a and can_drop_b:
                if vif_now[a] > vif_now[b]:
                    drop = a
                elif vif_now[b] > vif_now[a]:
                    drop = b
                else:
                    drop = a if entropy_full[a] < entropy_full[b] else b
            elif can_drop_a:
                drop = a
            else:
                drop = b

            retained.remove(drop)
            reasons[drop] = (
                f"dropped: |Spearman| with {b if drop == a else a} = {corr.loc[a,b]:.2f} >= "
                f"{self.redundancy_threshold}; VIF={vif_now[drop]:.2f}"
            )
            log.append(f"Removed {drop} (redundant with {b if drop == a else a}, rho={corr.loc[a,b]:.2f})")

        dropped = [c for c in candidates.columns if c not in retained]
        for c in dropped:
            reasons.setdefault(c, "dropped during selection")
        for c in retained:
            reasons.setdefault(c, "retained")

        sub = candidates[retained]
        spearman_after = sub.corr(method="spearman")
        vif_after = self._compute_vif(sub)

        kmo_overall, kmo_per_item, bartlett_chi2, bartlett_p = self._kmo_bartlett(sub)

        self.retained_ = retained
        self.dropped_ = dropped
        self.selection_ = {
            "spearman_before": spearman_before,
            "spearman_after": spearman_after,
            "vif_before": vif_before,
            "vif_after": vif_after,
            "entropy_full_set": entropy_full,
            "retained": retained,
            "dropped": dropped,
            "reasons": reasons,
            "log": log,
            "kmo_overall": kmo_overall,
            "kmo_per_item": kmo_per_item,
            "bartlett_chi2": bartlett_chi2,
            "bartlett_p": bartlett_p,
            "kmo_warning": (kmo_overall is not None and kmo_overall < 0.60),
        }
        return self.selection_

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
    # Section 5.1: dimension-balanced index (PRIMARY)
    # ------------------------------------------------------------------
    def _dimension_subscores(self, indicator_set):
        df = self.raw[indicator_set]
        subscores = {}
        weights_report = {}
        for dim, cols in DIMENSIONS.items():
            members = [c for c in cols if c in df.columns]
            if not members:
                continue
            if len(members) == 1:
                sub = self._minmax(df[members[0]])
                weights_report[dim] = {members[0]: 1.0}
            else:
                sub, w = self._topsis(df[members])
                weights_report[dim] = w.to_dict()
            subscores[dim] = sub
        sub_df = pd.DataFrame(subscores)
        return sub_df, weights_report

    def _snii_dim(self, indicator_set):
        sub_df, weights_report = self._dimension_subscores(indicator_set)
        n_dims = sub_df.shape[1]
        equal_w = 1.0 / n_dims
        # explicit equal-weight combination (0.25 each when all 4 dims present)
        snii_dim_raw = (sub_df * equal_w).sum(axis=1)
        return snii_dim_raw, sub_df, weights_report, equal_w

    # ------------------------------------------------------------------
    # Section 5.2: entropy-TOPSIS (robustness) + equal-weight TOPSIS
    # ------------------------------------------------------------------
    def _snii_topsis(self, indicator_set):
        df = self.raw[indicator_set]
        closeness, weights = self._topsis(df)
        equal_weights = pd.Series(1.0 / len(indicator_set), index=indicator_set)
        closeness_equal, _ = self._topsis(df, weights=equal_weights)
        return closeness, weights, closeness_equal

    # ------------------------------------------------------------------
    # Section 5.3: PCA
    # ------------------------------------------------------------------
    def _snii_pca(self, indicator_set):
        df = self.raw[indicator_set]
        Z = self._zscore(df)
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
            pca.components_[:comps_used].T,
            index=indicator_set,
            columns=[f"PC{i+1}" for i in range(comps_used)],
        )

        # orient each component so higher score = higher mean indicator value
        oriented_scores = scores[:, :comps_used].copy()
        mean_indicator = df.mean(axis=1).values
        for k in range(comps_used):
            corr_k = np.corrcoef(oriented_scores[:, k], mean_indicator)[0, 1]
            if corr_k < 0:
                oriented_scores[:, k] *= -1
                loadings.iloc[:, k] *= -1

        w = evr[:comps_used] / evr[:comps_used].sum()
        combined = (oriented_scores * w).sum(axis=1)
        combined_s = pd.Series(combined, index=df.index)
        snii_pca = self._minmax(combined_s)

        return snii_pca, evr, loadings, comps_used, eigenvalues

    # ------------------------------------------------------------------
    # discrimination diagnostics
    # ------------------------------------------------------------------
    @staticmethod
    def _gini(s: pd.Series) -> float:
        x = np.sort(s.values.astype(float))
        x = x - x.min() + 1e-12  # gini requires non-negative
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
    def _build_variant(self, indicator_set):
        snii_dim_raw, sub_df, dim_weights, equal_w = self._snii_dim(indicator_set)
        # Fix: min-max normalize primary SNII_DIM index to [0, 1] range
        snii_dim = self._minmax(snii_dim_raw)

        closeness_entropy, entropy_w, closeness_equal = self._snii_topsis(indicator_set)
        snii_pca, evr, loadings, comps_used, eigenvalues = self._snii_pca(indicator_set)

        combo = pd.concat([
            self._zscore(pd.DataFrame({"SNII_DIM": snii_dim})),
            self._zscore(pd.DataFrame({"SNII_TOPSIS": closeness_entropy})),
            self._zscore(pd.DataFrame({"SNII_PCA": snii_pca})),
        ], axis=1)
        snii_avg_z = combo.mean(axis=1)
        snii_avg = self._minmax(snii_avg_z)

        out = pd.DataFrame({
            "SNII_DIM": snii_dim,
            "SNII_TOPSIS": closeness_entropy,
            "SNII_TOPSIS_EQUAL": closeness_equal,
            "SNII_PCA": snii_pca,
            "SNII_AVG": snii_avg,
        })
        for c in sub_df.columns:
            out[c] = sub_df[c]

        for col in ["SNII_DIM", "SNII_TOPSIS", "SNII_TOPSIS_EQUAL", "SNII_PCA", "SNII_AVG"]:
            out[f"rank_{col}"] = out[col].rank(ascending=False, method="min").astype(int)
            out[f"{col}_pctrank"] = out[col].rank(pct=True)

        meta = {
            "dimension_weights": dim_weights,
            "equal_dimension_weight": equal_w,
            "entropy_weights": entropy_w.to_dict(),
            "pca_explained_variance_ratio": evr,
            "pca_eigenvalues": eigenvalues,
            "pca_loadings": loadings,
            "pca_components_used": comps_used,
            "distribution": {c: self._distribution_stats(out[c]) for c in
                               ["SNII_DIM", "SNII_TOPSIS", "SNII_TOPSIS_EQUAL", "SNII_PCA", "SNII_AVG"]},
        }
        return out, meta

    # ------------------------------------------------------------------
    def general_results(self) -> pd.DataFrame:
        if self.retained_ is None:
            self.select_indicators()
        out, meta = self._build_variant(self.retained_)
        self.results_selected_ = out
        self.meta_selected_ = meta
        return out

    def general_results_full(self) -> pd.DataFrame:
        out, meta = self._build_variant(ALL_INDICATORS)
        self.results_full_ = out
        self.meta_full_ = meta
        return out

    # ------------------------------------------------------------------
    # Section 7.2 / Section 8.4: size diagnostic
    # ------------------------------------------------------------------
    def size_diagnostic(self, raw_flow_comparison: pd.DataFrame = None) -> dict:
        if not hasattr(self, "results_selected_"):
            self.general_results()

        log_output = np.log(self.total_output.reindex(self.results_selected_.index))

        corr_rows = []
        snii_cols = ["SNII_DIM", "SNII_TOPSIS", "SNII_TOPSIS_EQUAL", "SNII_PCA", "SNII_AVG"]
        for col in snii_cols:
            s = self.results_selected_[col]
            corr_rows.append({
                "indicator": col,
                "spearman": s.corr(self.total_output, method="spearman"),
                "pearson": s.corr(self.total_output, method="pearson"),
            })
        for col in self.retained_:
            s = self.raw[col]
            corr_rows.append({
                "indicator": col,
                "spearman": s.corr(self.total_output, method="spearman"),
                "pearson": s.corr(self.total_output, method="pearson"),
            })
        size_corr = pd.DataFrame(corr_rows).set_index("indicator")

        raw_vs_coef = None
        if raw_flow_comparison is not None:
            pairs = [
                ("eigenvector_centrality", "eigenvector_centrality_raw"),
                ("pagerank", "pagerank_raw"),
                ("betweenness_centrality", "betweenness_centrality_raw"),
                ("hub_score", "hub_score_raw"),
                ("authority_score", "authority_score_raw"),
            ]
            rows = []
            for coef_col, raw_col in pairs:
                if coef_col in self.raw.columns and raw_col in raw_flow_comparison.columns:
                    coef_s = self.raw[coef_col]
                    raw_s = raw_flow_comparison[raw_col]
                    rows.append({
                        "indicator": coef_col,
                        "coef_based_spearman_vs_size": coef_s.corr(self.total_output, method="spearman"),
                        "raw_flow_spearman_vs_size": raw_s.corr(self.total_output, method="spearman"),
                    })
            raw_vs_coef = pd.DataFrame(rows).set_index("indicator")

        X = sm.add_constant(log_output)
        model = sm.OLS(self.results_selected_["SNII_DIM"].values, X.values).fit()
        residual = pd.Series(model.resid, index=self.results_selected_.index, name="SNII_DIM_size_controlled")
        residual_scaled = self._minmax(residual)

        top10_before = self.results_selected_["SNII_DIM"].sort_values(ascending=False).head(10).index.tolist()
        top10_after = residual_scaled.sort_values(ascending=False).head(10).index.tolist()

        return {
            "size_correlations": size_corr,
            "raw_vs_coefficient_size_correlation": raw_vs_coef,
            "ols_summary": model.summary(),
            "ols_r2": model.rsquared,
            "size_controlled_SNII": residual_scaled,
            "size_controlled_SNII_raw_residual": residual,
            "top10_before_size_control": top10_before,
            "top10_after_size_control": top10_after,
            "top10_overlap_count": len(set(top10_before) & set(top10_after)),
        }

    # ------------------------------------------------------------------
    # Section 8: robustness
    # ------------------------------------------------------------------
    def robustness_report(self) -> dict:
        if not hasattr(self, "results_selected_"):
            self.general_results()
        if not hasattr(self, "results_full_"):
            self.general_results_full()

        sel = self.results_selected_
        full = self.results_full_

        model_cols = ["SNII_DIM", "SNII_TOPSIS", "SNII_TOPSIS_EQUAL", "SNII_PCA"]
        model_corr = sel[model_cols].corr(method="spearman")

        indicator_set_corr = sel["SNII_DIM"].corr(full["SNII_DIM"], method="spearman")

        minmax_vs_rank_corr = {}
        for col in ["SNII_DIM", "SNII_TOPSIS", "SNII_PCA", "SNII_AVG"]:
            minmax_vs_rank_corr[col] = sel[col].corr(sel[f"{col}_pctrank"], method="spearman")

        size_diag = self.size_diagnostic()

        top10_sets = {
            "SNII_DIM": set(sel["SNII_DIM"].sort_values(ascending=False).head(10).index),
            "SNII_TOPSIS": set(sel["SNII_TOPSIS"].sort_values(ascending=False).head(10).index),
            "SNII_TOPSIS_EQUAL": set(sel["SNII_TOPSIS_EQUAL"].sort_values(ascending=False).head(10).index),
            "SNII_PCA": set(sel["SNII_PCA"].sort_values(ascending=False).head(10).index),
            "SNII_DIM_full": set(full["SNII_DIM"].sort_values(ascending=False).head(10).index),
            "SNII_DIM_size_controlled": set(size_diag["top10_after_size_control"]),
        }
        common_all = set.intersection(*top10_sets.values())

        return {
            "model_rank_correlation": model_corr,
            "full_vs_selected_rank_correlation": indicator_set_corr,
            "minmax_vs_rank_normalization_correlation": minmax_vs_rank_corr,
            "size_vs_size_controlled_top10_overlap": size_diag["top10_overlap_count"],
            "top10_sets": top10_sets,
            "top10_common_across_all_variants": common_all,
        }


def cls_safe_std(df: pd.DataFrame) -> pd.DataFrame:
    std = df.std(ddof=0)
    std_safe = std.replace(0, 1.0)
    return (df - df.mean()) / std_safe
