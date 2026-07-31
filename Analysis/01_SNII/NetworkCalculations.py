import numpy as np
import pandas as pd
import networkx as nx


class NetworkCalculations:
    """
    Builds the size-normalized production network from a 62-sector
    intermediate-input flow matrix and computes seven structural
    indicators grouped into four conceptual dimensions:

        D1 Direct linkage strength      -> backward_linkage, forward_linkage
        D2 Systemic embeddedness        -> eigenvector_centrality, pagerank
        D3 Bridge / spillover role      -> betweenness_centrality
        D4 Supply-distribution role     -> hub_score, authority_score

    All centralities are computed on size-normalized matrices:
        A (technical-coefficient, demand side, a_ij = z_ij / X_j) for
        demand-oriented measures, and
        B (Ghosh allocation, supply side, b_ij = z_ij / X_i) for
        supply-oriented measures. Raw flows z_ij are kept only for the
        mandatory size-comparison diagnostic (Section 3, "Comparison
        artifact").
    """

    EPS = 1e-10

    def __init__(self, filepath: str, sheet_name=0):
        raw = pd.read_excel(filepath, sheet_name=sheet_name, header=0, index_col=0)

        last_col = raw.columns[-1]
        if "total" not in str(last_col).lower():
            raise ValueError(
                f"Expected last column to be 'Total Output', found '{last_col}'."
            )

        self.sectors = list(raw.index)
        n_sectors = len(self.sectors)

        flow_cols = raw.columns[:-1]
        if list(flow_cols) != self.sectors and len(flow_cols) != n_sectors:
            raise ValueError("Flow-matrix columns do not match sector labels/count.")

        self.Z = raw[flow_cols].copy()
        self.Z.columns = self.sectors
        self.Z.index = self.sectors
        self.Z = self.Z.astype(float)

        self.total_output = raw[last_col].astype(float).copy()
        self.total_output.index = self.sectors
        self.total_output.name = "total_output"

        if (self.total_output <= 0).any():
            raise ValueError("Total output must be strictly positive for all sectors.")

        self.n = n_sectors

        # --- size-normalized matrices -------------------------------------
        # A: technical-coefficient (demand side), a_ij = z_ij / X_j
        # column j is normalized by purchasing sector j's total output.
        X = self.total_output.values
        Z = self.Z.values
        self.A = pd.DataFrame(Z / X[np.newaxis, :], index=self.sectors, columns=self.sectors)

        # B: Ghosh allocation (supply side), b_ij = z_ij / X_i
        # row i is normalized by supplying sector i's total output.
        self.B = pd.DataFrame(Z / X[:, np.newaxis], index=self.sectors, columns=self.sectors)

        # --- directed weighted graphs ---------------------------------
        # Edge i -> j : supplier i sells to user j.
        # Default graph used by all centralities is the A-weighted graph;
        # G_B is used explicitly for supply-oriented measures (forward linkage).
        self.G_A = self._build_graph(self.A)
        self.G_B = self._build_graph(self.B)
        self.G_raw = self._build_graph(self.Z)  # comparison only

        self._connectivity_report = {}
        self._results = None
        self._raw_flow_comparison = None

    # ------------------------------------------------------------------
    def _build_graph(self, weight_df: pd.DataFrame) -> nx.DiGraph:
        G = nx.DiGraph()
        G.add_nodes_from(self.sectors)
        arr = weight_df.values
        for i, si in enumerate(self.sectors):
            for j, sj in enumerate(self.sectors):
                w = arr[i, j]
                if i != j and w > 0:
                    # edge supplier(i) -> user(j)
                    G.add_edge(si, sj, weight=float(w))
        return G

    @staticmethod
    def _minmax(series: pd.Series) -> pd.Series:
        lo, hi = series.min(), series.max()
        if np.isclose(hi, lo):
            # Edge case: constant criterion -> neutral contribution.
            return pd.Series(0.5, index=series.index)
        return (series - lo) / (hi - lo)

    # ------------------------------------------------------------------
    # D1 -- Direct linkage strength
    # ------------------------------------------------------------------
    def backward_linkage(self) -> pd.Series:
        """Column sums of the Leontief inverse L=(I-A)^-1, normalized.
        Demand-side: how much a sector pulls from the whole economy
        (per unit of its own output) once indirect rounds are included."""
        I = np.eye(self.n)
        L = np.linalg.inv(I - self.A.values)
        col_sums = L.sum(axis=0)
        s = pd.Series(col_sums, index=self.sectors, name="backward_linkage")
        return self._minmax(s)

    def forward_linkage(self) -> pd.Series:
        """Row sums of the Ghosh inverse G=(I-B)^-1, normalized.
        Supply-side: how much a sector's output is redistributed
        throughout the economy once indirect rounds are included."""
        I = np.eye(self.n)
        Ginv = np.linalg.inv(I - self.B.values)
        row_sums = Ginv.sum(axis=1)
        s = pd.Series(row_sums, index=self.sectors, name="forward_linkage")
        return self._minmax(s)

    # ------------------------------------------------------------------
    # D2 -- Systemic embeddedness / influence
    # ------------------------------------------------------------------
    def eigenvector_centrality(self) -> pd.Series:
        """Eigenvector centrality on the A-weighted directed graph.
        Connectivity guard: eigenvector centrality is only well defined
        (Perron-Frobenius) on a strongly connected graph. If the graph
        is not strongly connected, it is restricted to the largest
        strongly connected component (LSCC); all other sectors receive
        the minimum in-component score (neutral, not silently invalid)."""
        G = self.G_A
        is_strong = nx.is_strongly_connected(G)
        self._connectivity_report["strongly_connected"] = is_strong

        if is_strong:
            self._connectivity_report["eigenvector_method"] = "full graph (strongly connected)"
            ec = nx.eigenvector_centrality(G, weight="weight", max_iter=2000, tol=1e-10)
            s = pd.Series(ec, index=self.sectors)
        else:
            largest_scc = max(nx.strongly_connected_components(G), key=len)
            self._connectivity_report["eigenvector_method"] = (
                f"restricted to largest strongly connected component "
                f"({len(largest_scc)}/{self.n} sectors); remaining sectors set to min in-SCC score"
            )
            self._connectivity_report["scc_size"] = len(largest_scc)
            sub = G.subgraph(largest_scc)
            ec_sub = nx.eigenvector_centrality(sub, weight="weight", max_iter=2000, tol=1e-10)
            min_val = min(ec_sub.values())
            ec = {s_: ec_sub.get(s_, min_val) for s_ in self.sectors}
            s = pd.Series(ec, index=self.sectors)

        s.name = "eigenvector_centrality"
        return self._minmax(s)

    def pagerank(self) -> pd.Series:
        """PageRank (damping 0.85) on the A-weighted directed graph."""
        pr = nx.pagerank(self.G_A, alpha=0.85, weight="weight")
        s = pd.Series(pr, index=self.sectors, name="pagerank")
        return self._minmax(s)

    # ------------------------------------------------------------------
    # D3 -- Bridge / spillover role
    # ------------------------------------------------------------------
    def betweenness_centrality(self) -> pd.Series:
        """Weighted betweenness centrality on the A-weighted graph using
        distance d_ij = 1/(a_ij + eps), so stronger coefficients imply
        shorter (cheaper) paths, as required for shortest-path betweenness."""
        G = nx.DiGraph()
        G.add_nodes_from(self.sectors)
        for u, v, data in self.G_A.edges(data=True):
            G.add_edge(u, v, distance=1.0 / (data["weight"] + self.EPS))
        bc = nx.betweenness_centrality(G, weight="distance", normalized=True)
        s = pd.Series(bc, index=self.sectors, name="betweenness_centrality")
        return self._minmax(s)

    # ------------------------------------------------------------------
    # D4 -- Supply-distribution orientation
    # ------------------------------------------------------------------
    def hits_scores(self):
        """Hub and Authority scores via HITS on the A-weighted graph.
        Hub: sells broadly to many important buyers (points to authorities).
        Authority: is purchased broadly by many important hubs."""
        hubs, auth = nx.hits(self.G_A, max_iter=2000, tol=1e-10, normalized=True)
        hub_s = pd.Series(hubs, index=self.sectors, name="hub_score")
        auth_s = pd.Series(auth, index=self.sectors, name="authority_score")
        return self._minmax(hub_s), self._minmax(auth_s)

    # ------------------------------------------------------------------
    def general_network_results(self) -> pd.DataFrame:
        """Combined 62 x 7 DataFrame of size-neutral structural indicators."""
        backward = self.backward_linkage()
        forward = self.forward_linkage()
        eig = self.eigenvector_centrality()
        pr = self.pagerank()
        bc = self.betweenness_centrality()
        hub, auth = self.hits_scores()

        df = pd.concat([backward, forward, eig, pr, bc, hub, auth], axis=1)
        df.columns = [
            "backward_linkage", "forward_linkage", "eigenvector_centrality", "pagerank",
            "betweenness_centrality", "hub_score", "authority_score",
        ]
        df.index.name = "sector"
        self._results = df
        return df

    # ------------------------------------------------------------------
    def raw_flow_comparison(self) -> pd.DataFrame:
        """Raw-flow (z_ij) versions of the four size-sensitive centralities,
        computed for comparison only (Section 3, mandatory comparison
        artifact). Never used inside the SNII itself."""
        G = self.G_raw
        is_strong = nx.is_strongly_connected(G)
        if is_strong:
            ec = nx.eigenvector_centrality(G, weight="weight", max_iter=2000, tol=1e-10)
        else:
            largest_scc = max(nx.strongly_connected_components(G), key=len)
            sub = G.subgraph(largest_scc)
            ec_sub = nx.eigenvector_centrality(sub, weight="weight", max_iter=2000, tol=1e-10)
            min_val = min(ec_sub.values())
            ec = {s_: ec_sub.get(s_, min_val) for s_ in self.sectors}

        pr = nx.pagerank(G, alpha=0.85, weight="weight")

        Gd = nx.DiGraph()
        Gd.add_nodes_from(self.sectors)
        for u, v, data in G.edges(data=True):
            Gd.add_edge(u, v, distance=1.0 / (data["weight"] + self.EPS))
        bc = nx.betweenness_centrality(Gd, weight="distance", normalized=True)

        hubs, auth = nx.hits(G, max_iter=2000, tol=1e-10, normalized=True)

        df = pd.DataFrame({
            "eigenvector_centrality_raw": pd.Series(ec, index=self.sectors),
            "pagerank_raw": pd.Series(pr, index=self.sectors),
            "betweenness_centrality_raw": pd.Series(bc, index=self.sectors),
            "hub_score_raw": pd.Series(hubs, index=self.sectors),
            "authority_score_raw": pd.Series(auth, index=self.sectors),
        })
        df.index.name = "sector"
        self._raw_flow_comparison = df
        return df

    # ------------------------------------------------------------------
    def connectivity_report(self) -> dict:
        """Strong-connectivity diagnostic; populated after eigenvector_centrality() runs."""
        if not self._connectivity_report:
            self.eigenvector_centrality()
        report = dict(self._connectivity_report)
        report["density"] = nx.density(self.G_A)
        report["edge_count"] = self.G_A.number_of_edges()
        report["node_count"] = self.G_A.number_of_nodes()
        return report
