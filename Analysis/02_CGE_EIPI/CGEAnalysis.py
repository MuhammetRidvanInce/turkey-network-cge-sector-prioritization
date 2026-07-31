import math
import time
import warnings

import numpy as np
import pandas as pd
from pyomo.environ import (
    ConcreteModel,
    Constraint,
    NonNegativeReals,
    Objective,
    Param,
    PositiveReals,
    Reals,
    Set,
    SolverFactory,
    Var,
    log,
    value,
)


class CGEModel:
    """
    SAM-calibrated computable general equilibrium (CGE) model for a 62-sector
    economy, built with Pyomo and solved with IPOPT.

    Structure (production: CES value added + Leontief intermediates; trade:
    CET exports/domestic supply and Armington imports/domestic demand;
    institutions: household/government/investment with the SAM's VAT blocks)
    follows CGEAnalysisExample.py, with one deliberate change mandated by the
    EIPI prompt (Section 1.1): the numeraire is an aggregate consumer price
    index (CPI), NOT the wage. Both the wage `w` and the capital rental `r`
    are fully endogenous. Fixing the wage would make aggregate labor income
    (w * Lbar) move one-for-one with the wage only, collapsing the functional
    labor share into a near-mechanical function of GDP; a price-index
    numeraire lets w and r respond independently to a sector shock.
    """

    EPSILON = 1e-12

    SECTOR_CATEGORIES = {
        **{f"sec{i}": "Agriculture" for i in range(1, 4)},
        **{f"sec{i}": "Industry" for i in range(4, 28)},
        **{f"sec{i}": "Service" for i in range(28, 63)},
    }
    CATEGORY_ELASTICITIES = {
        "Agriculture": {"psi": 1.12, "sigma": 1.81, "omega": 0.678},
        "Service": {"psi": 0.5, "sigma": 0.8, "omega": 0.4},
        "Industry": {"psi": 0.601, "sigma": 0.4, "omega": 0.678},
    }

    def __init__(self, sam_path="CGEModel-SAM.xlsx", sheet_name=0, solver_name="ipopt",
                 omega=None, psi=None, sigma=None, numeraire="absorption_price_index"):
        # By default, elasticities vary across the three broad NACE groups.
        # Each argument may alternatively be a scalar (uniform across sectors)
        # or a complete {sector: value} mapping for sensitivity experiments.
        self.sam_path = sam_path
        self.sheet_name = sheet_name
        self.solver_name = solver_name
        if numeraire not in {"absorption_price_index", "fixed_wage"}:
            raise ValueError("numeraire must be 'absorption_price_index' or 'fixed_wage'")
        self.numeraire = numeraire
        self.omega = omega
        self.psi = psi
        self.sigma = sigma

        self.sam = None
        self.sectors = None
        self.vat_account_map = None
        self.params = None
        self.benchmark = None
        self.baseline_output = None

        self.model = None
        self.baseline_values = None
        self.baseline_indicators = None
        self.baseline_report = None

    # ------------------------------------------------------------------
    # SAM loading helpers
    # ------------------------------------------------------------------
    def _load_sam(self):
        sam = pd.read_excel(self.sam_path, sheet_name=self.sheet_name, header=0, index_col=0)
        row_sums = sam.sum(axis=1)
        col_sums = sam.sum(axis=0)
        max_imbalance = float((row_sums - col_sums).abs().max())
        if max_imbalance > 1e-3 * float(sam.values.max()):
            warnings.warn(f"SAM row/column sums differ by up to {max_imbalance:.4f}; check balance.")
        return sam

    @staticmethod
    def _infer_sectors(sam: pd.DataFrame):
        vat_accounts = [acc for acc in sam.index if isinstance(acc, str) and acc.startswith("vat_")]
        non_sector = {
            "lab", "cap", "inctax", "svat", "protax", "hh", "gov", "sav",
            "imp", "total", "inv", "exp", *vat_accounts,
        }
        return [account for account in sam.columns if account not in non_sector]

    @staticmethod
    def _resolve_vat_account_map(sam: pd.DataFrame, sectors):
        vat_index = set(sam.index)
        mapping = {}
        for sector in sectors:
            candidate = f"vat_{sector}"
            if candidate not in vat_index:
                raise KeyError(f"Expected VAT account '{candidate}' for sector '{sector}' not found in SAM.")
            mapping[sector] = candidate
        return mapping

    @staticmethod
    def _safe_ratio(numerator, denominator):
        return float(numerator / denominator) if abs(denominator) > CGEModel.EPSILON else 0.0

    @classmethod
    def default_elasticity_map(cls, parameter):
        if parameter not in {"psi", "sigma", "omega"}:
            raise KeyError(f"Unknown elasticity parameter: {parameter}")
        return {
            sector: cls.CATEGORY_ELASTICITIES[category][parameter]
            for sector, category in cls.SECTOR_CATEGORIES.items()
        }

    @classmethod
    def _resolve_elasticity(cls, specification, sectors, parameter):
        if specification is None:
            resolved = cls.default_elasticity_map(parameter)
        elif np.isscalar(specification):
            resolved = {sector: float(specification) for sector in sectors}
        else:
            resolved = {sector: float(specification[sector]) for sector in sectors}

        missing = sorted(set(sectors) - set(resolved))
        if missing:
            raise KeyError(f"{parameter} elasticity mapping is missing sectors: {missing}")
        invalid = {sector: value for sector, value in resolved.items() if value <= 0}
        if invalid:
            raise ValueError(f"{parameter} elasticities must be positive: {invalid}")
        return {sector: float(resolved[sector]) for sector in sectors}

    # ------------------------------------------------------------------
    # Section 2.1: calibrate
    # ------------------------------------------------------------------
    def calibrate(self):
        """Calibrate all share/scale parameters from the SAM so the model's
        benchmark (zero-shock) solution reproduces the SAM exactly."""
        self.sam = self._load_sam()
        sam = self.sam
        sectors = self._infer_sectors(sam)
        vat_account_map = self._resolve_vat_account_map(sam, sectors)
        self.sectors = sectors
        self.vat_account_map = vat_account_map

        unknown_sectors = sorted(set(sectors) - set(self.SECTOR_CATEGORIES))
        if unknown_sectors and any(x is None for x in (self.psi, self.sigma, self.omega)):
            raise KeyError(f"Default category mapping is missing sectors: {unknown_sectors}")
        psi0 = self._resolve_elasticity(self.psi, sectors, "psi")
        sigma0 = self._resolve_elasticity(self.sigma, sectors, "sigma")
        omega0 = self._resolve_elasticity(self.omega, sectors, "omega")
        pwe0 = {s: 1.0 for s in sectors}
        pwm0 = {s: 1.0 for s in sectors}
        epsilon0 = 1.0

        lbar = float(sam.loc["lab", "total"])
        kbar = float(sam.loc["cap", "total"])

        px0 = {s: 1.0 for s in sectors}
        pz0 = {s: 1.0 for s in sectors}
        pe0 = {s: 1.0 for s in sectors}
        pd0 = {s: 1.0 for s in sectors}
        pq0 = {s: 1.0 for s in sectors}
        pm0 = {s: 1.0 for s in sectors}
        r0 = 1.0
        w0 = 1.0

        y0 = float(sam.loc["hh", "total"])
        l0 = {s: float(sam.loc["lab", s]) for s in sectors}
        k0 = {s: float(sam.loc["cap", s]) for s in sectors}
        x0 = {s: l0[s] + k0[s] for s in sectors}

        i0 = {(su, us): float(sam.loc[su, us]) for su in sectors for us in sectors}
        z0 = {s: x0[s] + sum(i0[(su, s)] for su in sectors) for s in sectors}

        e0 = {s: float(sam.loc[s, "exp"]) for s in sectors}
        m0 = {s: float(sam.loc["imp", s]) for s in sectors}

        td0 = float(sam.loc["inctax", "hh"])
        tva0_sector = {s: float(sam.loc["svat", s]) for s in sectors}
        tvh0_sector = {s: float(sam.loc[vat_account_map[s], "hh"]) for s in sectors}
        tvg0_sector = {s: float(sam.loc[vat_account_map[s], "gov"]) for s in sectors}
        tvi0_sector = {s: float(sam.loc[vat_account_map[s], "inv"]) for s in sectors}
        tz0_sector = {s: float(sam.loc["protax", s]) for s in sectors}

        tva0 = sum(tva0_sector.values())
        tvh0 = sum(tvh0_sector.values())
        tvg0 = sum(tvg0_sector.values())
        tvi0 = sum(tvi0_sector.values())
        tz0 = sum(tz0_sector.values())
        t0 = tva0 + tz0 + td0 + tvh0 + tvg0 + tvi0

        d0 = {s: z0[s] + tva0_sector[s] + tz0_sector[s] - e0[s] for s in sectors}

        c0 = {s: float(sam.loc[s, "hh"]) for s in sectors}
        g0 = {s: float(sam.loc[s, "gov"]) for s in sectors}
        inv0 = {s: float(sam.loc[s, "inv"]) for s in sectors}

        q0 = {s: c0[s] + g0[s] + inv0[s] + sum(i0[(s, us)] for us in sectors) for s in sectors}

        sp0 = float(sam.loc["sav", "hh"])
        sg0 = float(sam.loc["sav", "gov"])
        sf0 = float(sam.loc["sav", "exp"])
        s0 = sp0 + sg0 + sf0
        yd0 = y0 - sp0 - td0

        td = self._safe_ratio(td0, y0)
        tvh = {s: self._safe_ratio(tvh0_sector[s], c0[s]) for s in sectors}
        tvg = {s: self._safe_ratio(tvg0_sector[s], g0[s]) for s in sectors}
        tvi = {s: self._safe_ratio(tvi0_sector[s], inv0[s]) for s in sectors}

        pch0 = {s: (1.0 + tvh[s]) * pq0[s] for s in sectors}
        pcg0 = {s: (1.0 + tvg[s]) * pq0[s] for s in sectors}
        pci0 = {s: (1.0 + tvi[s]) * pq0[s] for s in sectors}

        tva = {s: self._safe_ratio(tva0_sector[s], z0[s]) for s in sectors}
        tz = {s: self._safe_ratio(tz0_sector[s], z0[s]) for s in sectors}

        alpha = {s: (omega0[s] - 1.0) / omega0[s] for s in sectors}
        delta, beta, aprod = {}, {}, {}
        for s in sectors:
            labor_term = max(l0[s], self.EPSILON) ** (1.0 - alpha[s])
            capital_term = max(k0[s], self.EPSILON) ** (1.0 - alpha[s])
            total_term = labor_term + capital_term
            delta[s] = labor_term / total_term
            beta[s] = capital_term / total_term
            ces_term = (
                delta[s] * max(l0[s], self.EPSILON) ** alpha[s]
                + beta[s] * max(k0[s], self.EPSILON) ** alpha[s]
            ) ** (1.0 / alpha[s])
            aprod[s] = self._safe_ratio(x0[s], ces_term)

        a = {(su, us): self._safe_ratio(i0[(su, us)], z0[us]) for su in sectors for us in sectors}
        x1 = {s: self._safe_ratio(x0[s], z0[s]) for s in sectors}

        rho = {s: (psi0[s] + 1.0) / psi0[s] for s in sectors}
        eta = {s: (sigma0[s] - 1.0) / sigma0[s] for s in sectors}

        e_share, dt_share, theta = {}, {}, {}
        m_share, da_share, lambda_ = {}, {}, {}
        for s in sectors:
            export_term = max(e0[s], self.EPSILON) ** (1.0 - rho[s])
            domestic_term = max(d0[s], self.EPSILON) ** (1.0 - rho[s])
            cet_total = export_term + domestic_term
            e_share[s] = export_term / cet_total
            dt_share[s] = domestic_term / cet_total
            theta[s] = self._safe_ratio(
                z0[s],
                (e_share[s] * max(e0[s], self.EPSILON) ** rho[s]
                 + dt_share[s] * max(d0[s], self.EPSILON) ** rho[s]) ** (1.0 / rho[s]),
            )

            import_term = max(m0[s], self.EPSILON) ** (1.0 - eta[s])
            armington_domestic_term = max(d0[s], self.EPSILON) ** (1.0 - eta[s])
            armington_total = import_term + armington_domestic_term
            m_share[s] = import_term / armington_total
            da_share[s] = armington_domestic_term / armington_total
            if abs(eta[s]) < self.EPSILON:
                # sigma == 1 is the Cobb-Douglas limit of the Armington CES.
                # Handling the limit explicitly avoids the undefined 1/eta
                # expression in both calibration and model construction.
                armington_quantity = (
                    max(m0[s], self.EPSILON) ** m_share[s]
                    * max(d0[s], self.EPSILON) ** da_share[s]
                )
            else:
                armington_quantity = (
                    m_share[s] * max(m0[s], self.EPSILON) ** eta[s]
                    + da_share[s] * max(d0[s], self.EPSILON) ** eta[s]
                ) ** (1.0 / eta[s])
            lambda_[s] = self._safe_ratio(q0[s], armington_quantity)

        c = {s: self._safe_ratio(pch0[s] * c0[s], yd0) for s in sectors}
        g = {s: self._safe_ratio(pcg0[s] * g0[s], t0 - sg0) for s in sectors}
        inv = {s: self._safe_ratio(pci0[s] * inv0[s], s0) for s in sectors}
        sp = self._safe_ratio(sp0, y0)
        sg = self._safe_ratio(sg0, t0)

        # Absorption (economy-wide) weights for the numeraire price index --
        # deliberately BROADER than, and distinct from, the household-only
        # consumption weights `c` used for the reported CPI indicator
        # (Section 3). See build_model()/cpi_numeraire_rule for why the two
        # must differ: pinning the exact same index used as the reported
        # "CPI" indicator would make CPI_pct_change identically zero in
        # every scenario by construction.
        q_total = sum(q0.values()) or 1.0
        q_share = {s: q0[s] / q_total for s in sectors}

        self.params = {
            "lbar": lbar, "kbar": kbar, "pwe": pwe0, "pwm": pwm0, "epsilon0": epsilon0,
            "psi": psi0, "sigma": sigma0, "omega": omega0,
            "td": td, "tvh": tvh, "tvg": tvg, "tvi": tvi, "tva": tva, "tz": tz,
            "alpha": alpha, "delta": delta, "beta": beta, "aprod": aprod,
            "a": a, "x1": x1, "rho": rho, "eta": eta,
            "e_share": e_share, "dt_share": dt_share, "theta": theta,
            "m_share": m_share, "da_share": da_share, "lambda_": lambda_,
            "c": c, "g": g, "inv": inv, "sp": sp, "sg": sg, "pch0": pch0, "q_share": q_share,
        }

        self.benchmark = {
            "y0": y0, "l0": l0, "k0": k0, "x0": x0, "i0": i0, "z0": z0,
            "e0": e0, "m0": m0, "d0": d0, "c0": c0, "g0": g0, "inv0": inv0, "q0": q0,
            "td0": td0, "tvh0": tvh0, "tvg0": tvg0, "tvi0": tvi0, "tva0": tva0, "tz0": tz0,
            "tvh0_sector": tvh0_sector, "tvg0_sector": tvg0_sector, "tvi0_sector": tvi0_sector,
            "tva0_sector": tva0_sector, "tz0_sector": tz0_sector,
            "sp0": sp0, "sg0": sg0, "sf0": sf0, "s0": s0, "t0": t0, "yd0": yd0,
            "px0": px0, "pz0": pz0, "pd0": pd0, "pq0": pq0,
            "pch0": pch0, "pcg0": pcg0, "pci0": pci0, "pe0": pe0, "pm0": pm0,
            "r0": r0, "w0": w0,
        }

        self.baseline_output = pd.Series(z0, name="baseline_output").reindex(sectors)
        return self.params, self.benchmark

    # ------------------------------------------------------------------
    # Section 2.2: build_model
    # ------------------------------------------------------------------
    def build_model(self):
        """Construct the Pyomo model: sets, parameters, variables, constraints,
        and the CPI numeraire. `Aprod` is mutable and is the shockable TFP
        parameter used by shock_model()."""
        if self.params is None:
            self.calibrate()
        params = self.params
        base = self.benchmark
        sectors = self.sectors

        m = ConcreteModel()
        m.i = Set(initialize=sectors, ordered=True)
        m.j = Set(initialize=sectors, ordered=True)

        m.Lbar = Param(initialize=params["lbar"], mutable=True)
        m.Kbar = Param(initialize=params["kbar"], mutable=True)
        m.Pwe = Param(m.i, initialize=params["pwe"])
        m.Pwm = Param(m.i, initialize=params["pwm"])
        m.E0 = Param(initialize=0.0)
        m.epsilon = Param(initialize=params["epsilon0"], mutable=True)

        m.td = Param(initialize=params["td"], mutable=True)
        m.tvh = Param(m.i, initialize=params["tvh"], mutable=True)
        m.tvg = Param(m.i, initialize=params["tvg"], mutable=True)
        m.tvi = Param(m.i, initialize=params["tvi"], mutable=True)
        m.tva = Param(m.i, initialize=params["tva"])
        m.tz = Param(m.i, initialize=params["tz"])
        m.alpha = Param(m.i, initialize=params["alpha"])
        m.delta = Param(m.i, initialize=params["delta"])
        m.beta = Param(m.i, initialize=params["beta"])
        m.Aprod = Param(m.i, initialize=params["aprod"], mutable=True)
        m.a = Param(m.j, m.i, initialize=params["a"])
        m.x1 = Param(m.i, initialize=params["x1"])
        m.rho = Param(m.i, initialize=params["rho"])
        m.eta = Param(m.i, initialize=params["eta"])
        m.e_share = Param(m.i, initialize=params["e_share"])
        m.dt_share = Param(m.i, initialize=params["dt_share"])
        m.theta = Param(m.i, initialize=params["theta"])
        m.m_share = Param(m.i, initialize=params["m_share"])
        m.da_share = Param(m.i, initialize=params["da_share"])
        m.lambda_ = Param(m.i, initialize=params["lambda_"])
        m.c = Param(m.i, initialize=params["c"])
        m.g = Param(m.i, initialize=params["g"])
        m.inv = Param(m.i, initialize=params["inv"])
        m.sp = Param(initialize=params["sp"])
        m.sg = Param(initialize=params["sg"])
        # base household consumer prices (used only for reporting the CPI indicator)
        m.pch0 = Param(m.i, initialize=params["pch0"])
        # economy-wide absorption weights anchoring the numeraire (Section 1.1)
        m.q_share = Param(m.i, initialize=params["q_share"])

        m.Y = Var(domain=PositiveReals, initialize=base["y0"])
        m.L = Var(m.i, domain=NonNegativeReals, initialize=base["l0"])
        m.K = Var(m.i, domain=NonNegativeReals, initialize=base["k0"])
        m.X = Var(m.i, domain=NonNegativeReals, initialize=base["x0"])
        m.I = Var(m.j, m.i, domain=Reals, initialize=base["i0"])
        m.Z = Var(m.i, domain=NonNegativeReals, initialize=base["z0"])
        m.E = Var(m.i, domain=NonNegativeReals, initialize=base["e0"])
        m.M = Var(m.i, domain=NonNegativeReals, initialize=base["m0"])

        m.D = Var(m.i, domain=NonNegativeReals, initialize=base["d0"])
        m.C = Var(m.i, domain=NonNegativeReals, initialize=base["c0"])
        m.G = Var(m.i, domain=NonNegativeReals, initialize=base["g0"])
        m.INV = Var(m.j, domain=Reals, initialize=base["inv0"])
        m.Q = Var(m.i, domain=NonNegativeReals, initialize=base["q0"])

        m.T = Var(domain=NonNegativeReals, initialize=base["t0"])
        m.Td = Var(domain=NonNegativeReals, initialize=base["td0"])
        m.Tvh = Var(domain=NonNegativeReals, initialize=base["tvh0"])
        m.Tvg = Var(domain=NonNegativeReals, initialize=base["tvg0"])
        m.Tvi = Var(domain=NonNegativeReals, initialize=base["tvi0"])
        m.Tva = Var(domain=Reals, initialize=base["tva0"])
        m.Tz = Var(domain=Reals, initialize=base["tz0"])

        m.Tvhs = Var(m.i, domain=NonNegativeReals, initialize=base["tvh0_sector"])
        m.Tvgs = Var(m.i, domain=NonNegativeReals, initialize=base["tvg0_sector"])
        m.Tvis = Var(m.i, domain=Reals, initialize=base["tvi0_sector"])
        m.Tvas = Var(m.i, domain=Reals, initialize=base["tva0_sector"])
        m.Tzs = Var(m.i, domain=Reals, initialize=base["tz0_sector"])

        m.Sp = Var(domain=NonNegativeReals, initialize=base["sp0"])
        m.Sg = Var(domain=NonNegativeReals, initialize=base["sg0"])
        m.Sf = Var(domain=Reals, initialize=base["sf0"])
        m.S = Var(domain=NonNegativeReals, initialize=base["s0"])
        m.Yd = Var(domain=PositiveReals, initialize=base["yd0"])

        m.px = Var(m.i, domain=PositiveReals, initialize=base["px0"])
        m.pz = Var(m.i, domain=PositiveReals, initialize=base["pz0"])
        m.pd = Var(m.i, domain=PositiveReals, initialize=base["pd0"])
        m.pq = Var(m.i, domain=PositiveReals, initialize=base["pq0"])
        m.pch = Var(m.i, domain=PositiveReals, initialize=base["pch0"])
        m.pcg = Var(m.i, domain=PositiveReals, initialize=base["pcg0"])
        m.pci = Var(m.i, domain=PositiveReals, initialize=base["pci0"])
        m.pe = Var(m.i, domain=PositiveReals, initialize=base["pe0"])
        m.pm = Var(m.i, domain=PositiveReals, initialize=base["pm0"])
        # w (wage) and r (capital rental) are BOTH endogenous (Section 1.1) --
        # neither is fixed. The single price normalization needed for
        # determinacy is supplied by the CPI numeraire constraint below.
        m.r = Var(domain=PositiveReals, initialize=base["r0"])
        m.w = Var(domain=PositiveReals, initialize=base["w0"])
        if self.numeraire == "fixed_wage":
            m.w.fix(base["w0"])

        def production_rule(mod, s):
            return mod.X[s] == mod.Aprod[s] * (
                mod.delta[s] * mod.L[s] ** mod.alpha[s] + mod.beta[s] * mod.K[s] ** mod.alpha[s]
            ) ** (1.0 / mod.alpha[s])

        def labor_demand_rule(mod, s):
            return mod.L[s] == mod.px[s] * mod.X[s] / (
                mod.w + mod.r * ((mod.delta[s] * mod.r) / (mod.beta[s] * mod.w)) ** (1.0 / (mod.alpha[s] - 1.0))
            )

        def capital_demand_rule(mod, s):
            return mod.K[s] == mod.px[s] * mod.X[s] / (
                mod.r + mod.w * ((mod.beta[s] * mod.w) / (mod.delta[s] * mod.r)) ** (1.0 / (mod.alpha[s] - 1.0))
            )

        def intermediate_input_rule(mod, su, us):
            return mod.I[su, us] == mod.a[su, us] * mod.Z[us]

        def activity_output_rule(mod, s):
            return mod.X[s] == mod.x1[s] * mod.Z[s]

        def producer_price_rule(mod, s):
            return mod.pz[s] == mod.px[s] * mod.x1[s] + sum(mod.a[su, s] * mod.pq[su] for su in mod.j)

        def cet_rule(mod, s):
            return mod.Z[s] == mod.theta[s] * (
                mod.e_share[s] * mod.E[s] ** mod.rho[s] + mod.dt_share[s] * mod.D[s] ** mod.rho[s]
            ) ** (1.0 / mod.rho[s])

        def export_supply_rule(mod, s):
            return mod.E[s] == (
                (mod.theta[s] ** mod.rho[s] * mod.e_share[s] * (1.0 + mod.tz[s] + mod.tva[s]) * mod.pz[s] / mod.pe[s])
                ** (1.0 / (1.0 - mod.rho[s]))
            ) * mod.Z[s]

        def domestic_supply_rule(mod, s):
            return mod.D[s] == (
                (mod.theta[s] ** mod.rho[s] * mod.dt_share[s] * (1.0 + mod.tz[s] + mod.tva[s]) * mod.pz[s] / mod.pd[s])
                ** (1.0 / (1.0 - mod.rho[s]))
            ) * mod.Z[s]

        def armington_rule(mod, s):
            if abs(value(mod.eta[s])) < self.EPSILON:
                return mod.Q[s] == mod.lambda_[s] * (
                    mod.M[s] ** mod.m_share[s] * mod.D[s] ** mod.da_share[s]
                )
            return mod.Q[s] == mod.lambda_[s] * (
                mod.m_share[s] * mod.M[s] ** mod.eta[s] + mod.da_share[s] * mod.D[s] ** mod.eta[s]
            ) ** (1.0 / mod.eta[s])

        def import_demand_rule(mod, s):
            return mod.M[s] == (
                (mod.lambda_[s] ** mod.eta[s] * mod.m_share[s] * mod.pq[s] / mod.pm[s]) ** (1.0 / (1.0 - mod.eta[s]))
            ) * mod.Q[s]

        def domestic_armington_rule(mod, s):
            return mod.D[s] == (
                (mod.lambda_[s] ** mod.eta[s] * mod.da_share[s] * mod.pq[s] / mod.pd[s]) ** (1.0 / (1.0 - mod.eta[s]))
            ) * mod.Q[s]

        def household_consumer_price_rule(mod, s):
            return mod.pch[s] == (1.0 + mod.tvh[s]) * mod.pq[s]

        def government_consumer_price_rule(mod, s):
            return mod.pcg[s] == (1.0 + mod.tvg[s]) * mod.pq[s]

        def investment_consumer_price_rule(mod, s):
            return mod.pci[s] == (1.0 + mod.tvi[s]) * mod.pq[s]

        def household_demand_rule(mod, s):
            return mod.C[s] == (mod.c[s] / mod.pch[s]) * mod.Yd

        def income_rule(mod):
            return mod.Y == mod.w * mod.Lbar + mod.r * mod.Kbar

        def disposable_income_rule(mod):
            return mod.Yd == mod.Y - mod.Sp - mod.Td

        def government_demand_rule(mod, s):
            return mod.G[s] == (mod.g[s] / mod.pcg[s]) * (mod.T - mod.Sg)

        def total_tax_rule(mod):
            return mod.T == mod.Td + mod.Tz + mod.Tva + mod.Tvh + mod.Tvg + mod.Tvi

        def direct_tax_rule(mod):
            return mod.Td == mod.td * mod.Y

        def household_sector_vat_rule(mod, s):
            return mod.Tvhs[s] == (mod.pch[s] - mod.pq[s]) * mod.C[s]

        def total_household_vat_rule(mod):
            return mod.Tvh == sum(mod.Tvhs[s] for s in mod.i)

        def government_sector_vat_rule(mod, s):
            return mod.Tvgs[s] == (mod.pcg[s] - mod.pq[s]) * mod.G[s]

        def total_government_vat_rule(mod):
            return mod.Tvg == sum(mod.Tvgs[s] for s in mod.i)

        def investment_sector_vat_rule(mod, s):
            return mod.Tvis[s] == (mod.pci[s] - mod.pq[s]) * mod.INV[s]

        def total_investment_vat_rule(mod):
            return mod.Tvi == sum(mod.Tvis[s] for s in mod.i)

        def sector_production_tax_rule(mod, s):
            return mod.Tzs[s] == mod.tz[s] * mod.pz[s] * mod.Z[s]

        def total_production_tax_rule(mod):
            return mod.Tz == sum(mod.Tzs[s] for s in mod.i)

        def sector_value_added_tax_rule(mod, s):
            return mod.Tvas[s] == mod.tva[s] * mod.pz[s] * mod.Z[s]

        def total_value_added_tax_rule(mod):
            return mod.Tva == sum(mod.Tvas[s] for s in mod.i)

        def investment_demand_rule(mod, s):
            return mod.INV[s] == (mod.inv[s] / mod.pci[s]) * mod.S

        def total_savings_rule(mod):
            return mod.S == mod.Sp + mod.Sg + mod.Sf * mod.epsilon

        def private_savings_rule(mod):
            return mod.Sp == mod.sp * mod.Y

        def government_savings_rule(mod):
            return mod.Sg == mod.sg * mod.T

        def export_price_rule(mod, s):
            return mod.pe[s] == mod.epsilon * mod.Pwe[s]

        def import_price_rule(mod, s):
            return mod.pm[s] == mod.epsilon * mod.Pwm[s]

        def foreign_balance_rule(mod):
            return sum(mod.Pwe[s] * mod.E[s] for s in mod.i) + mod.E0 + mod.Sf == sum(
                mod.Pwm[s] * mod.M[s] for s in mod.i
            )

        last_sector = sectors[-1]

        def commodity_market_rule(mod, s):
            # One commodity-market equation is dropped by Walras's law for
            # numerical determinacy (Section 1.3); its residual is checked
            # explicitly in solve_model().
            if s == last_sector:
                return Constraint.Skip
            return mod.Q[s] == mod.C[s] + mod.G[s] + mod.INV[s] + sum(mod.I[s, us] for us in mod.j)

        def labor_market_rule(mod):
            return sum(mod.L[s] for s in mod.i) == mod.Lbar

        def capital_market_rule(mod):
            return sum(mod.K[s] for s in mod.i) == mod.Kbar

        def cpi_numeraire_rule(mod):
            # Price-index numeraire (Section 1.1): a geometric, base-year
            # ABSORPTION-weighted (q_share) index of producer/composite
            # prices `pq`, held at its benchmark level (pq0 == 1 for all
            # sectors, so log(pq0)==0). This is the ONE price normalization
            # the model needs (homogeneity of degree zero in prices); it
            # leaves BOTH w and r free to move with a sector shock, so the
            # wage-based mechanical link between GDP and labor income
            # (Section 1.2) is broken.
            #
            # This numeraire is deliberately BROADER than, and distinct
            # from, the household-consumption-weighted CPI reported as an
            # EIPI indicator in Section 3 (which uses `pch`/`c`-shares).
            # Pinning the exact same index used as the reported CPI would
            # make CPI_pct_change identically zero in every counterfactual
            # by construction -- a degenerate indicator. With two DIFFERENT
            # weighting schemes (broad absorption vs. narrow household
            # consumption), a sector shock that moves relative prices still
            # shows up as a genuine, non-zero change in the reported CPI
            # even though the broader numeraire index is fixed.
            if self.numeraire == "fixed_wage":
                return Constraint.Skip
            return sum(mod.q_share[s] * log(mod.pq[s]) for s in mod.i) == 0

        m.production_eq = Constraint(m.i, rule=production_rule)
        m.labor_demand_eq = Constraint(m.i, rule=labor_demand_rule)
        m.capital_demand_eq = Constraint(m.i, rule=capital_demand_rule)
        m.intermediate_input_eq = Constraint(m.j, m.i, rule=intermediate_input_rule)
        m.activity_output_eq = Constraint(m.i, rule=activity_output_rule)
        m.producer_price_eq = Constraint(m.i, rule=producer_price_rule)
        m.cet_eq = Constraint(m.i, rule=cet_rule)
        m.export_supply_eq = Constraint(m.i, rule=export_supply_rule)
        m.domestic_supply_eq = Constraint(m.i, rule=domestic_supply_rule)
        m.armington_eq = Constraint(m.i, rule=armington_rule)
        m.import_demand_eq = Constraint(m.i, rule=import_demand_rule)
        m.domestic_armington_eq = Constraint(m.i, rule=domestic_armington_rule)
        m.household_consumer_price_eq = Constraint(m.i, rule=household_consumer_price_rule)
        m.government_consumer_price_eq = Constraint(m.i, rule=government_consumer_price_rule)
        m.investment_consumer_price_eq = Constraint(m.i, rule=investment_consumer_price_rule)
        m.household_demand_eq = Constraint(m.i, rule=household_demand_rule)
        m.income_eq = Constraint(rule=income_rule)
        m.disposable_income_eq = Constraint(rule=disposable_income_rule)
        m.government_demand_eq = Constraint(m.i, rule=government_demand_rule)
        m.total_tax_eq = Constraint(rule=total_tax_rule)
        m.direct_tax_eq = Constraint(rule=direct_tax_rule)
        m.household_sector_vat_eq = Constraint(m.i, rule=household_sector_vat_rule)
        m.total_household_vat_eq = Constraint(rule=total_household_vat_rule)
        m.government_sector_vat_eq = Constraint(m.i, rule=government_sector_vat_rule)
        m.total_government_vat_eq = Constraint(rule=total_government_vat_rule)
        m.investment_sector_vat_eq = Constraint(m.i, rule=investment_sector_vat_rule)
        m.total_investment_vat_eq = Constraint(rule=total_investment_vat_rule)
        m.sector_production_tax_eq = Constraint(m.i, rule=sector_production_tax_rule)
        m.total_production_tax_eq = Constraint(rule=total_production_tax_rule)
        m.sector_value_added_tax_eq = Constraint(m.i, rule=sector_value_added_tax_rule)
        m.total_value_added_tax_eq = Constraint(rule=total_value_added_tax_rule)
        m.investment_demand_eq = Constraint(m.i, rule=investment_demand_rule)
        m.total_savings_eq = Constraint(rule=total_savings_rule)
        m.private_savings_eq = Constraint(rule=private_savings_rule)
        m.government_savings_eq = Constraint(rule=government_savings_rule)
        m.export_price_eq = Constraint(m.i, rule=export_price_rule)
        m.import_price_eq = Constraint(m.i, rule=import_price_rule)
        m.foreign_balance_eq = Constraint(rule=foreign_balance_rule)
        m.commodity_market_eq = Constraint(m.i, rule=commodity_market_rule)
        m.labor_market_eq = Constraint(rule=labor_market_rule)
        m.capital_market_eq = Constraint(rule=capital_market_rule)
        m.cpi_numeraire_eq = Constraint(rule=cpi_numeraire_rule)

        m.obj = Objective(expr=0.0)

        self.model = m
        self._last_sector = last_sector
        return m

    # ------------------------------------------------------------------
    # internal solve / bookkeeping helpers
    # ------------------------------------------------------------------
    def _solver(self):
        return SolverFactory(self.solver_name)

    def _solve(self, tee=False):
        solver = self._solver()
        start = time.perf_counter()
        results = solver.solve(self.model, tee=tee)
        elapsed = time.perf_counter() - start
        status = str(results.solver.status)
        termination = str(results.solver.termination_condition)
        if termination.lower() not in {"optimal", "locallyoptimal", "feasible"}:
            warnings.warn(f"Solver returned status={status}, termination={termination}.", RuntimeWarning)
        return {"status": status, "termination": termination, "solve_time": elapsed}

    def _capture_values(self):
        values = {}
        for var in self.model.component_objects(Var, active=True):
            if var.is_indexed():
                for idx in var:
                    values[(var.name, idx)] = value(var[idx])
            else:
                values[(var.name, None)] = value(var)
        return values

    @staticmethod
    def _get(values_dict, name, idx=None):
        return float(values_dict[(name, idx)])

    def _load_solution_as_start(self, solution_values):
        for var in self.model.component_objects(Var, active=True):
            if var.is_indexed():
                for idx in var:
                    key = (var.name, idx)
                    if key in solution_values:
                        var[idx].set_value(solution_values[key])
            else:
                key = (var.name, None)
                if key in solution_values:
                    var.set_value(solution_values[key])

    def _reset_productivity(self):
        base_aprod = self.params["aprod"]
        for s in self.sectors:
            self.model.Aprod[s].set_value(base_aprod[s])

    def _budget_shares(self):
        shares = self.params["c"]
        total = sum(shares.values()) or 1.0
        return {s: shares[s] / total for s in self.sectors}

    def _walras_residual(self, values_dict):
        s = self._last_sector
        lhs = self._get(values_dict, "Q", s)
        rhs = (
            self._get(values_dict, "C", s) + self._get(values_dict, "G", s) + self._get(values_dict, "INV", s)
            + sum(self._get(values_dict, "I", (s, us)) for us in self.sectors)
        )
        return lhs - rhs

    # ------------------------------------------------------------------
    # indicator computation (Section 3)
    # ------------------------------------------------------------------
    def _price_qty_sum(self, values_dict, price_name, qty_name):
        return float(sum(
            self._get(values_dict, price_name, s) * self._get(values_dict, qty_name, s) for s in self.sectors
        ))

    def _fixed_price_qty_sum(self, values_dict, base_values_dict, price_name, qty_name):
        return float(sum(
            self._get(base_values_dict, price_name, s) * self._get(values_dict, qty_name, s) for s in self.sectors
        ))

    def _real_gdp(self, values_dict, base_values_dict):
        # GDP definition (Section 1.9): REAL GDP at fixed base-year prices,
        # expenditure side C + I + G + X - M (Laspeyres-type fixed-price sum).
        return (
            self._fixed_price_qty_sum(values_dict, base_values_dict, "pch", "C")
            + self._fixed_price_qty_sum(values_dict, base_values_dict, "pcg", "G")
            + self._fixed_price_qty_sum(values_dict, base_values_dict, "pci", "INV")
            + self._fixed_price_qty_sum(values_dict, base_values_dict, "pe", "E")
            - self._fixed_price_qty_sum(values_dict, base_values_dict, "pm", "M")
        )

    def _household_utility(self, values_dict, shares):
        return math.exp(sum(
            shares[s] * math.log(max(self._get(values_dict, "C", s), self.EPSILON)) for s in self.sectors
        ))

    def _cobb_douglas_expenditure(self, price_dict, utility_level, shares):
        return utility_level * math.exp(sum(
            shares[s] * math.log(max(price_dict[s] / shares[s], self.EPSILON)) for s in self.sectors
        ))

    def _cpi(self, values_dict, base_values_dict, shares):
        # Cobb-Douglas exact (geometric) price index; base = 100.
        return 100.0 * math.exp(sum(
            shares[s] * math.log(max(
                self._get(values_dict, "pch", s) / self._get(base_values_dict, "pch", s), self.EPSILON
            )) for s in self.sectors
        ))

    def _compute_indicators(self, values_dict, base_values_dict, shocked_sector=None):
        """Compute the seven raw (level) EIPI inputs for one solved state
        (baseline or counterfactual). `base_values_dict` supplies the FIXED
        prices used for the real-GDP / CPI comparison (always the calibrated
        baseline solution, never the counterfactual)."""
        shares = self._budget_shares()

        household_consumption = self._price_qty_sum(values_dict, "pch", "C")
        government_consumption = self._price_qty_sum(values_dict, "pcg", "G")
        investment = self._price_qty_sum(values_dict, "pci", "INV")
        exports = self._price_qty_sum(values_dict, "pe", "E")
        imports = self._price_qty_sum(values_dict, "pm", "M")
        absorption = self._price_qty_sum(values_dict, "pq", "Q")

        real_gdp = self._real_gdp(values_dict, base_values_dict)
        nominal_gdp = household_consumption + government_consumption + investment + exports - imports

        w = self._get(values_dict, "w")
        r = self._get(values_dict, "r")
        Lbar = value(self.model.Lbar)
        Kbar = value(self.model.Kbar)
        labor_income = w * Lbar
        capital_income = r * Kbar
        # Functional labor share (Section 1.2): wL / (wL + rK), evaluated at
        # ENDOGENOUS w, r. Because both prices float under the CPI numeraire,
        # this ratio is free to move independently of real GDP.
        labor_income_share = self._safe_ratio(labor_income, labor_income + capital_income)

        base_price_dict = {s: self._get(base_values_dict, "pch", s) for s in self.sectors}
        base_utility = self._household_utility(base_values_dict, shares)
        current_utility = self._household_utility(values_dict, shares)
        # Welfare (Section 1.9): Hicksian EQUIVALENT VARIATION, expressed as a
        # % of baseline household income (documented choice, not a raw utility level).
        equivalent_variation = self._cobb_douglas_expenditure(base_price_dict, current_utility, shares) - \
            self._cobb_douglas_expenditure(base_price_dict, base_utility, shares)
        base_income = self._get(base_values_dict, "Y")
        welfare = self._safe_ratio(equivalent_variation, base_income) * 100.0

        export_share = self._safe_ratio(exports, nominal_gdp) * 100.0

        # ImportDependency (Section 3): M / (Q + M - X), all nominal
        # aggregates (Q = total nominal absorption of Armington composite
        # goods, which already nets imports in; adding M and subtracting X
        # follows the prompt's literal formula for an import-penetration-style
        # ratio). Falls back to M/GDP if the denominator is degenerate.
        denom = absorption + imports - exports
        if abs(denom) > self.EPSILON:
            import_dependency = self._safe_ratio(imports, denom) * 100.0
        else:
            import_dependency = self._safe_ratio(imports, nominal_gdp) * 100.0

        cpi = self._cpi(values_dict, base_values_dict, shares)

        sector_output = {s: self._get(values_dict, "Z", s) for s in self.sectors}
        base_sector_output = {s: self._get(base_values_dict, "Z", s) for s in self.sectors}
        if shocked_sector is not None:
            non_shocked = [s for s in self.sectors if s != shocked_sector]
            spillover_change = sum(sector_output[s] - base_sector_output[s] for s in non_shocked)
            non_shocked_base = sum(base_sector_output[s] for s in non_shocked)
            spillover_pct = self._safe_ratio(spillover_change, non_shocked_base) * 100.0
        else:
            spillover_pct = 0.0

        return {
            "GDP": real_gdp,
            "Welfare": welfare,
            "ExportShare": export_share,
            "LaborIncomeShare": labor_income_share,
            "Spillover": spillover_pct,
            "ImportDependency": import_dependency,
            "CPI": cpi,
        }

    # ------------------------------------------------------------------
    # Section 2.3: solve_model
    # ------------------------------------------------------------------
    def solve_model(self, tee=False):
        """Solve the calibrated baseline, verify it reproduces the SAM, and
        check the Walras-law residual of the dropped commodity equation."""
        if self.model is None:
            self.build_model()
        self._reset_productivity()
        solve_info = self._solve(tee=tee)
        values = self._capture_values()

        base_z = pd.Series({s: values[("Z", s)] for s in self.sectors})
        sam_z = pd.Series(self.benchmark["z0"])
        max_deviation = float((base_z - sam_z).abs().max())
        max_deviation_pct = float(((base_z - sam_z).abs() / sam_z.replace(0, np.nan)).max() * 100.0)

        walras_residual = self._walras_residual(values)

        indicators = self._compute_indicators(values, values, shocked_sector=None)

        self.baseline_values = values
        self.baseline_indicators = indicators
        self.baseline_report = {
            "solver_status": solve_info["status"],
            "solver_termination": solve_info["termination"],
            "solve_time_seconds": solve_info["solve_time"],
            "max_abs_deviation_from_SAM": max_deviation,
            "max_pct_deviation_from_SAM": max_deviation_pct,
            "walras_residual": walras_residual,
            "indicators": indicators,
            "w": values[("w", None)],
            "r": values[("r", None)],
        }
        return self.baseline_report

    # ------------------------------------------------------------------
    # Section 2.4: shock_model
    # ------------------------------------------------------------------
    def shock_model(self, sector, shock_size=0.10, tee=False):
        """Reset Aprod to the calibrated baseline, then apply a productivity
        shock to exactly one sector: A_sector^cf = A_sector^base * (1 + shock_size).

        NOTE on signature: the prompt's abbreviated method list shows
        `shock_model(self, shock_size=0.10)`. Because "apply the shock to one
        sector" (Section 1.4/2.4) is not expressible without naming that
        sector, this implementation adds an explicit `sector` argument -- the
        method name and all other required names are unchanged. `shock_size`
        may still be a scalar OR a {sector: size} mapping (Section 2.4's
        "per-sector mapping" support for the size-normalized robustness
        variant in Section 1.5); when a mapping is given, `shock_size[sector]`
        is used.
        """
        if self.model is None:
            self.build_model()
        if self.baseline_values is None:
            self.solve_model()

        self._load_solution_as_start(self.baseline_values)
        self._reset_productivity()

        magnitude = shock_size[sector] if isinstance(shock_size, dict) else shock_size
        base_aprod = self.params["aprod"]
        self.model.Aprod[sector].set_value(base_aprod[sector] * (1.0 + magnitude))

        solve_info = self._solve(tee=tee)
        values = self._capture_values()
        indicators = self._compute_indicators(values, self.baseline_values, shocked_sector=sector)

        return {
            "sector": sector,
            "shock_size": magnitude,
            "values": values,
            "indicators": indicators,
            "solver_status": solve_info["status"],
            "solver_termination": solve_info["termination"],
            "solve_time_seconds": solve_info["solve_time"],
        }

    # ------------------------------------------------------------------
    # Section 2.5: counterfactual_result
    # ------------------------------------------------------------------
    def counterfactual_result(self, shock_size=0.10, tee=False, sectors=None):
        """Iterate the productivity shock over all sectors (or a supplied
        subset, e.g. for a faster sensitivity pass), solving each and
        computing the seven EIPI indicators. Returns a 62 x 21 DataFrame:
        for every indicator, `<name>_base`, `<name>_counterfactual`,
        `<name>_pct_change`."""
        if self.baseline_indicators is None:
            self.solve_model(tee=tee)

        target_sectors = sectors if sectors is not None else self.sectors
        indicator_names = ["GDP", "Welfare", "ExportShare", "LaborIncomeShare", "Spillover",
                            "ImportDependency", "CPI"]

        rows = {}
        statuses = {}
        for s in target_sectors:
            shock_result = self.shock_model(s, shock_size=shock_size, tee=tee)
            statuses[s] = (shock_result["solver_status"], shock_result["solver_termination"])
            row = {}
            for name in indicator_names:
                base_val = self.baseline_indicators[name]
                cf_val = shock_result["indicators"][name]
                row[f"{name}_base"] = base_val
                row[f"{name}_counterfactual"] = cf_val
                if name in ("Spillover", "Welfare"):
                    # Spillover and Welfare (Hicksian EV, already expressed
                    # as a % of baseline household income) have no
                    # meaningful non-zero BASE level -- baseline-vs-baseline
                    # is zero by definition. The counterfactual value IS
                    # already the pct-change-style measure requested in
                    # Section 3, so pct_change == counterfactual value
                    # (dividing by a base of 0 would otherwise force every
                    # pct_change to 0, silently degenerating the indicator).
                    row[f"{name}_pct_change"] = cf_val
                else:
                    row[f"{name}_pct_change"] = self._safe_ratio(cf_val - base_val, abs(base_val)) * 100.0
            rows[s] = row

        result = pd.DataFrame.from_dict(rows, orient="index")
        result.index.name = "sector"
        result = result.reindex(target_sectors)
        result.attrs["solver_statuses"] = statuses
        self._reset_productivity()
        return result
