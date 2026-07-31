"""Class-based CGE calibration, sector shocks, and CGE Impact Score (CIS) analysis."""

from __future__ import annotations

import math
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd
from pyomo.environ import (
    ConcreteModel,
    Constraint,
    NonNegativeReals,
    Objective,
    log,
    Param,
    PositiveReals,
    Reals,
    Set,
    SolverFactory,
    Var,
    value,
)



EPSILON = 1e-12


def load_sam(path, sheet_name=0):
    sam = pd.read_excel(path, sheet_name=sheet_name, index_col=0)
    sam.index = sam.index.astype(str).str.strip()
    sam.columns = sam.columns.astype(str).str.strip()
    return sam.fillna(0.0)


def resolve_vat_account_map(sam: pd.DataFrame, sectors: Sequence[str]) -> Dict[str, str]:
    vat_accounts = [acc for acc in sam.index if isinstance(acc, str) and acc.startswith("vat_")]
    mapping = {}
    for sector in sectors:
        candidate = f"vat_{sector}"
        if candidate in sam.index:
            mapping[sector] = candidate
        elif vat_accounts:
            suffix = sector.replace("sec", "")
            match = [acc for acc in vat_accounts if acc.endswith(suffix)]
            mapping[sector] = match[0] if match else vat_accounts[0]
        else:
            raise ValueError("VAT accounts could not be resolved from the SAM.")
    return mapping


def _safe_ratio(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if abs(denominator) > EPSILON else 0.0


def _infer_sectors_from_sam(sam: pd.DataFrame) -> List[str]:
    vat_accounts = [acc for acc in sam.index if isinstance(acc, str) and acc.startswith("vat_")]
    non_sector = {
        "lab",
        "cap",
        "inctax",
        "svat",
        "protax",
        "hh",
        "gov",
        "sav",
        "imp",
        "total",
        "inv",
        "exp",
        *vat_accounts,
    }
    return [account for account in sam.columns if account not in non_sector]


@dataclass
class CalibrationData:
    sectors: List[str]
    vat_account_map: Dict[str, str]
    params: Dict[str, object]
    benchmark: Dict[str, object]


@dataclass
class ScenarioResult:
    name: str
    shocked_sectors: List[str]
    shock_size: float
    solve_time_seconds: float
    solver_status: str
    termination_condition: str
    solution_values: Dict[tuple, float]
    macro_results: pd.Series
    macro_comparison: pd.DataFrame
    macro_changes: pd.Series
    sector_output_changes: pd.DataFrame


@dataclass
class CGEImpactSummary:
    """Container for the sector-by-sector CGE impact scoring outputs."""

    baseline: ScenarioResult
    scenario_change_table: pd.DataFrame
    impact_table: pd.DataFrame
    top10_cis: pd.DataFrame


class CGEModel:
    """Calibrate, solve, and simulate the 62-sector CGE model from the reference file."""

    def __init__(
        self,
        sam_path: str | Path,
        sheet_name: str | int = 0,
        solver_name: str = "ipopt",
        psi: float = 1.12,
        sigma: float = 1.81,
        omega: float = 0.678,
    ) -> None:
        self.sam_path = Path(sam_path)
        self.sam = load_sam(self.sam_path, sheet_name=sheet_name)
        self.solver_name = solver_name
        self.psi = psi
        self.sigma = sigma
        self.omega = omega

        self.calibration = self._calibrate_from_sam()
        self.sectors = self.calibration.sectors
        self.model: Optional[ConcreteModel] = None
        self.baseline_result: Optional[ScenarioResult] = None

    def _calibrate_from_sam(self) -> CalibrationData:
        sam = self.sam
        sectors = _infer_sectors_from_sam(sam)
        vat_account_map = resolve_vat_account_map(sam, sectors)

        pwe0 = {sector: 1.0 for sector in sectors}
        pwm0 = {sector: 1.0 for sector in sectors}
        epsilon0 = 1.0
        psi0 = {sector: float(self.psi) for sector in sectors}
        sigma0 = {sector: float(self.sigma) for sector in sectors}
        omega0 = {sector: float(self.omega) for sector in sectors}

        lbar = float(sam.loc["lab", "total"])
        kbar = float(sam.loc["cap", "total"])

        px0 = {sector: 1.0 for sector in sectors}
        pz0 = {sector: 1.0 for sector in sectors}
        pe0 = {sector: 1.0 for sector in sectors}
        pd0 = {sector: 1.0 for sector in sectors}
        pq0 = {sector: 1.0 for sector in sectors}
        pm0 = {sector: 1.0 for sector in sectors}
        r0 = 1.0
        w0 = 1.0

        y0 = float(sam.loc["hh", "total"])
        l0 = {sector: float(sam.loc["lab", sector]) for sector in sectors}
        k0 = {sector: float(sam.loc["cap", sector]) for sector in sectors}
        x0 = {sector: l0[sector] + k0[sector] for sector in sectors}

        i0 = {(supplier, user): float(sam.loc[supplier, user]) for supplier in sectors for user in sectors}
        z0 = {sector: x0[sector] + sum(i0[(supplier, sector)] for supplier in sectors) for sector in sectors}

        e0 = {sector: float(sam.loc[sector, "exp"]) for sector in sectors}
        m0 = {sector: float(sam.loc["imp", sector]) for sector in sectors}

        td0 = float(sam.loc["inctax", "hh"])
        tva0_sector = {sector: float(sam.loc["svat", sector]) for sector in sectors}
        tvh0_sector = {sector: float(sam.loc[vat_account_map[sector], "hh"]) for sector in sectors}
        tvg0_sector = {sector: float(sam.loc[vat_account_map[sector], "gov"]) for sector in sectors}
        tvi0_sector = {sector: float(sam.loc[vat_account_map[sector], "inv"]) for sector in sectors}
        tz0_sector = {sector: float(sam.loc["protax", sector]) for sector in sectors}

        tva0 = sum(tva0_sector.values())
        tvh0 = sum(tvh0_sector.values())
        tvg0 = sum(tvg0_sector.values())
        tvi0 = sum(tvi0_sector.values())
        tz0 = sum(tz0_sector.values())
        t0 = tva0 + tz0 + td0 + tvh0 + tvg0 + tvi0

        d0 = {
            sector: z0[sector] + tva0_sector[sector] + tz0_sector[sector] - e0[sector]
            for sector in sectors
        }

        c0 = {sector: float(sam.loc[sector, "hh"]) for sector in sectors}
        g0 = {sector: float(sam.loc[sector, "gov"]) for sector in sectors}
        inv0 = {sector: float(sam.loc[sector, "inv"]) for sector in sectors}

        q0 = {
            sector: c0[sector] + g0[sector] + inv0[sector] + sum(i0[(sector, user)] for user in sectors)
            for sector in sectors
        }

        sp0 = float(sam.loc["sav", "hh"])
        sg0 = float(sam.loc["sav", "gov"])
        sf0 = float(sam.loc["sav", "exp"])
        s0 = sp0 + sg0 + sf0
        yd0 = y0 - sp0 - td0

        td = _safe_ratio(td0, y0)
        tvh = {sector: _safe_ratio(tvh0_sector[sector], c0[sector]) for sector in sectors}
        tvg = {sector: _safe_ratio(tvg0_sector[sector], g0[sector]) for sector in sectors}
        tvi = {sector: _safe_ratio(tvi0_sector[sector], inv0[sector]) for sector in sectors}

        pch0 = {sector: (1.0 + tvh[sector]) * pq0[sector] for sector in sectors}
        pcg0 = {sector: (1.0 + tvg[sector]) * pq0[sector] for sector in sectors}
        pci0 = {sector: (1.0 + tvi[sector]) * pq0[sector] for sector in sectors}

        tva = {sector: _safe_ratio(tva0_sector[sector], z0[sector]) for sector in sectors}
        tz = {sector: _safe_ratio(tz0_sector[sector], z0[sector]) for sector in sectors}

        alpha = {sector: (omega0[sector] - 1.0) / omega0[sector] for sector in sectors}
        delta = {}
        beta = {}
        aprod = {}
        for sector in sectors:
            labor_term = max(l0[sector], EPSILON) ** (1.0 - alpha[sector])
            capital_term = max(k0[sector], EPSILON) ** (1.0 - alpha[sector])
            total_term = labor_term + capital_term
            delta[sector] = labor_term / total_term
            beta[sector] = capital_term / total_term
            ces_term = (
                delta[sector] * max(l0[sector], EPSILON) ** alpha[sector]
                + beta[sector] * max(k0[sector], EPSILON) ** alpha[sector]
            ) ** (1.0 / alpha[sector])
            aprod[sector] = _safe_ratio(x0[sector], ces_term)

        a = {(supplier, user): _safe_ratio(i0[(supplier, user)], z0[user]) for supplier in sectors for user in sectors}
        x1 = {sector: _safe_ratio(x0[sector], z0[sector]) for sector in sectors}

        rho = {sector: (psi0[sector] + 1.0) / psi0[sector] for sector in sectors}
        eta = {sector: (sigma0[sector] - 1.0) / sigma0[sector] for sector in sectors}

        e_share = {}
        dt_share = {}
        theta = {}
        m_share = {}
        da_share = {}
        lambda_ = {}
        for sector in sectors:
            export_term = max(e0[sector], EPSILON) ** (1.0 - rho[sector])
            domestic_term = max(d0[sector], EPSILON) ** (1.0 - rho[sector])
            cet_total = export_term + domestic_term
            e_share[sector] = export_term / cet_total
            dt_share[sector] = domestic_term / cet_total
            theta[sector] = _safe_ratio(
                z0[sector],
                (
                    e_share[sector] * max(e0[sector], EPSILON) ** rho[sector]
                    + dt_share[sector] * max(d0[sector], EPSILON) ** rho[sector]
                )
                ** (1.0 / rho[sector]),
            )

            import_term = max(m0[sector], EPSILON) ** (1.0 - eta[sector])
            armington_domestic_term = max(d0[sector], EPSILON) ** (1.0 - eta[sector])
            armington_total = import_term + armington_domestic_term
            m_share[sector] = import_term / armington_total
            da_share[sector] = armington_domestic_term / armington_total
            lambda_[sector] = _safe_ratio(
                q0[sector],
                (
                    m_share[sector] * max(m0[sector], EPSILON) ** eta[sector]
                    + da_share[sector] * max(d0[sector], EPSILON) ** eta[sector]
                )
                ** (1.0 / eta[sector]),
            )

        c = {sector: _safe_ratio(pch0[sector] * c0[sector], yd0) for sector in sectors}
        g = {sector: _safe_ratio(pcg0[sector] * g0[sector], t0 - sg0) for sector in sectors}
        inv = {sector: _safe_ratio(pci0[sector] * inv0[sector], s0) for sector in sectors}
        sp = _safe_ratio(sp0, y0)
        sg = _safe_ratio(sg0, t0)

        params = {
            "lbar": lbar,
            "kbar": kbar,
            "pwe": pwe0,
            "pwm": pwm0,
            "epsilon0": epsilon0,
            "td": td,
            "tvh": tvh,
            "tvg": tvg,
            "tvi": tvi,
            "tva": tva,
            "tz": tz,
            "alpha": alpha,
            "delta": delta,
            "beta": beta,
            "aprod": aprod,
            "a": a,
            "x1": x1,
            "rho": rho,
            "eta": eta,
            "e_share": e_share,
            "dt_share": dt_share,
            "theta": theta,
            "m_share": m_share,
            "da_share": da_share,
            "lambda_": lambda_,
            "c": c,
            "g": g,
            "inv": inv,
            "sp": sp,
            "sg": sg,
        }

        benchmark = {
            "y0": y0,
            "l0": l0,
            "k0": k0,
            "x0": x0,
            "i0": i0,
            "z0": z0,
            "e0": e0,
            "m0": m0,
            "d0": d0,
            "c0": c0,
            "g0": g0,
            "inv0": inv0,
            "q0": q0,
            "td0": td0,
            "tvh0": tvh0,
            "tvg0": tvg0,
            "tvi0": tvi0,
            "tva0": tva0,
            "tz0": tz0,
            "tvh0_sector": tvh0_sector,
            "tvg0_sector": tvg0_sector,
            "tvi0_sector": tvi0_sector,
            "tva0_sector": tva0_sector,
            "tz0_sector": tz0_sector,
            "sp0": sp0,
            "sg0": sg0,
            "sf0": sf0,
            "s0": s0,
            "t0": t0,
            "yd0": yd0,
            "px0": px0,
            "pz0": pz0,
            "pd0": pd0,
            "pq0": pq0,
            "pch0": pch0,
            "pcg0": pcg0,
            "pci0": pci0,
            "pe0": pe0,
            "pm0": pm0,
            "r0": r0,
            "w0": w0,
        }

        return CalibrationData(sectors=sectors, vat_account_map=vat_account_map, params=params, benchmark=benchmark)

    def build_model(self) -> ConcreteModel:
        calib = self.calibration
        params = calib.params
        base = calib.benchmark

        m = ConcreteModel()
        m.i = Set(initialize=calib.sectors, ordered=True)
        m.j = Set(initialize=calib.sectors, ordered=True)

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
        m.INV = Var(m.i, domain=Reals, initialize=base["inv0"])
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
        m.r = Var(domain=PositiveReals, initialize=base["r0"])
        m.w = Var(domain=PositiveReals, initialize=base["w0"])

        def production_rule(model, sector):
            return model.X[sector] == model.Aprod[sector] * (
                model.delta[sector] * model.L[sector] ** model.alpha[sector]
                + model.beta[sector] * model.K[sector] ** model.alpha[sector]
            ) ** (1.0 / model.alpha[sector])

        def labor_demand_rule(model, sector):
            return model.L[sector] == (
                model.px[sector]
                * model.X[sector]
                / (
                    model.w
                    + model.r
                    * ((model.delta[sector] * model.r) / (model.beta[sector] * model.w))
                    ** (1.0 / (model.alpha[sector] - 1.0))
                )
            )

        def capital_demand_rule(model, sector):
            return model.K[sector] == (
                model.px[sector]
                * model.X[sector]
                / (
                    model.r
                    + model.w
                    * ((model.beta[sector] * model.w) / (model.delta[sector] * model.r))
                    ** (1.0 / (model.alpha[sector] - 1.0))
                )
            )

        def intermediate_input_rule(model, supplier, user):
            return model.I[supplier, user] == model.a[supplier, user] * model.Z[user]

        def activity_output_rule(model, sector):
            return model.X[sector] == model.x1[sector] * model.Z[sector]

        def producer_price_rule(model, sector):
            return model.pz[sector] == model.px[sector] * model.x1[sector] + sum(
                model.a[supplier, sector] * model.pq[supplier] for supplier in model.j
            )

        def cet_rule(model, sector):
            return model.Z[sector] == model.theta[sector] * (
                model.e_share[sector] * model.E[sector] ** model.rho[sector]
                + model.dt_share[sector] * model.D[sector] ** model.rho[sector]
            ) ** (1.0 / model.rho[sector])

        def export_supply_rule(model, sector):
            return model.E[sector] == (
                (
                    model.theta[sector] ** model.rho[sector]
                    * model.e_share[sector]
                    * (1.0 + model.tz[sector] + model.tva[sector])
                    * model.pz[sector]
                    / model.pe[sector]
                )
                ** (1.0 / (1.0 - model.rho[sector]))
            ) * model.Z[sector]

        def domestic_supply_rule(model, sector):
            return model.D[sector] == (
                (
                    model.theta[sector] ** model.rho[sector]
                    * model.dt_share[sector]
                    * (1.0 + model.tz[sector] + model.tva[sector])
                    * model.pz[sector]
                    / model.pd[sector]
                )
                ** (1.0 / (1.0 - model.rho[sector]))
            ) * model.Z[sector]

        def armington_rule(model, sector):
            return model.Q[sector] == model.lambda_[sector] * (
                model.m_share[sector] * model.M[sector] ** model.eta[sector]
                + model.da_share[sector] * model.D[sector] ** model.eta[sector]
            ) ** (1.0 / model.eta[sector])

        def import_demand_rule(model, sector):
            return model.M[sector] == (
                (
                    model.lambda_[sector] ** model.eta[sector]
                    * model.m_share[sector]
                    * model.pq[sector]
                    / model.pm[sector]
                )
                ** (1.0 / (1.0 - model.eta[sector]))
            ) * model.Q[sector]

        def domestic_armington_rule(model, sector):
            return model.D[sector] == (
                (
                    model.lambda_[sector] ** model.eta[sector]
                    * model.da_share[sector]
                    * model.pq[sector]
                    / model.pd[sector]
                )
                ** (1.0 / (1.0 - model.eta[sector]))
            ) * model.Q[sector]

        def household_consumer_price_rule(model, sector):
            return model.pch[sector] == (1.0 + model.tvh[sector]) * model.pq[sector]

        def government_consumer_price_rule(model, sector):
            return model.pcg[sector] == (1.0 + model.tvg[sector]) * model.pq[sector]

        def investment_consumer_price_rule(model, sector):
            return model.pci[sector] == (1.0 + model.tvi[sector]) * model.pq[sector]

        def household_demand_rule(model, sector):
            return model.C[sector] == (model.c[sector] / model.pch[sector]) * model.Yd

        def income_rule(model):
            return model.Y == model.w * model.Lbar + model.r * model.Kbar

        def disposable_income_rule(model):
            return model.Yd == model.Y - model.Sp - model.Td

        def government_demand_rule(model, sector):
            return model.G[sector] == (model.g[sector] / model.pcg[sector]) * (model.T - model.Sg)

        def total_tax_rule(model):
            return model.T == model.Td + model.Tz + model.Tva + model.Tvh + model.Tvg + model.Tvi

        def direct_tax_rule(model):
            return model.Td == model.td * model.Y

        def household_sector_vat_rule(model, sector):
            return model.Tvhs[sector] == (model.pch[sector] - model.pq[sector]) * model.C[sector]

        def total_household_vat_rule(model):
            return model.Tvh == sum(model.Tvhs[sector] for sector in model.i)

        def government_sector_vat_rule(model, sector):
            return model.Tvgs[sector] == (model.pcg[sector] - model.pq[sector]) * model.G[sector]

        def total_government_vat_rule(model):
            return model.Tvg == sum(model.Tvgs[sector] for sector in model.i)

        def investment_sector_vat_rule(model, sector):
            return model.Tvis[sector] == (model.pci[sector] - model.pq[sector]) * model.INV[sector]

        def total_investment_vat_rule(model):
            return model.Tvi == sum(model.Tvis[sector] for sector in model.i)

        def sector_production_tax_rule(model, sector):
            return model.Tzs[sector] == model.tz[sector] * model.pz[sector] * model.Z[sector]

        def total_production_tax_rule(model):
            return model.Tz == sum(model.Tzs[sector] for sector in model.i)

        def sector_value_added_tax_rule(model, sector):
            return model.Tvas[sector] == model.tva[sector] * model.pz[sector] * model.Z[sector]

        def total_value_added_tax_rule(model):
            return model.Tva == sum(model.Tvas[sector] for sector in model.i)

        def investment_demand_rule(model, sector):
            return model.INV[sector] == (model.inv[sector] / model.pci[sector]) * model.S

        def total_savings_rule(model):
            return model.S == model.Sp + model.Sg + model.Sf * model.epsilon

        def private_savings_rule(model):
            return model.Sp == model.sp * model.Y

        def government_savings_rule(model):
            return model.Sg == model.sg * model.T

        def export_price_rule(model, sector):
            return model.pe[sector] == model.epsilon * model.Pwe[sector]

        def import_price_rule(model, sector):
            return model.pm[sector] == model.epsilon * model.Pwm[sector]

        def foreign_balance_rule(model):
            return sum(model.Pwe[sector] * model.E[sector] for sector in model.i) + model.E0 + model.Sf == sum(
                model.Pwm[sector] * model.M[sector] for sector in model.i
            )

        last_sector = calib.sectors[-1]

        def commodity_market_rule(model, sector):
            if sector == last_sector:
                return Constraint.Skip
            return model.Q[sector] == model.C[sector] + model.G[sector] + model.INV[sector] + sum(
                model.I[sector, user] for user in model.j
            )

        def labor_market_rule(model):
            return sum(model.L[sector] for sector in model.i) == model.Lbar

        def capital_market_rule(model):
            return sum(model.K[sector] for sector in model.i) == model.Kbar

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

        cpi_shares = self._budget_shares()

        def cpi_numeraire_rule(model):
            return sum(
                cpi_shares[sector] * log(model.pch[sector] / base["pch0"][sector])
                for sector in model.i
            ) == 0.0

        # Price-index numeraire: wage and capital rental remain endogenous.
        m.numeraire = Constraint(rule=cpi_numeraire_rule)

        m.obj = Objective(expr=0.0)
        self.model = m
        return m

    def _solver(self):
        return SolverFactory(self.solver_name)

    @staticmethod
    def capture_var_values(model: ConcreteModel) -> Dict[tuple, float]:
        values = {}
        for var in model.component_objects(Var, active=True):
            if var.is_indexed():
                for idx in var:
                    values[(var.name, idx)] = value(var[idx])
            else:
                values[(var.name, None)] = value(var)
        return values

    @staticmethod
    def _get_var(values_dict: Mapping[tuple, float], var_name: str, idx=None) -> float:
        return float(values_dict[(var_name, idx)])

    def _load_solution_as_start(self, solution_values: Mapping[tuple, float]) -> None:
        if self.model is None:
            return
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

    def reset_productivity(self) -> None:
        if self.model is None:
            self.build_model()
        assert self.model is not None
        base_aprod = self.calibration.params["aprod"]
        for sector in self.sectors:
            self.model.Aprod[sector].set_value(base_aprod[sector])

    def apply_productivity_shock(self, sectors: Sequence[str], shock_size: float = 0.10) -> None:
        if self.model is None:
            self.build_model()
        assert self.model is not None
        self.reset_productivity()
        base_aprod = self.calibration.params["aprod"]
        for sector in sectors:
            self.model.Aprod[sector].set_value(base_aprod[sector] * (1.0 + shock_size))

    def solve(self, tee: bool = False):
        if self.model is None:
            self.build_model()
        solver = self._solver()
        start = time.perf_counter()
        results = solver.solve(self.model, tee=tee)
        elapsed = time.perf_counter() - start
        status = str(results.solver.status)
        termination = str(results.solver.termination_condition)
        if termination.lower() not in {"optimal", "locallyoptimal", "feasible"}:
            warnings.warn(
                f"Solver returned status={status}, termination={termination}.",
                RuntimeWarning,
            )
        return results, elapsed

    def _budget_shares(self) -> Dict[str, float]:
        shares = self.calibration.params["c"].copy()
        share_sum = sum(shares.values()) or 1.0
        return {sector: shares[sector] / share_sum for sector in self.sectors}

    def _price_quantity_sum(self, values_dict: Mapping[tuple, float], price_name: str, qty_name: str) -> float:
        return float(
            sum(self._get_var(values_dict, price_name, sector) * self._get_var(values_dict, qty_name, sector) for sector in self.sectors)
        )

    def _fixed_price_quantity_sum(
        self,
        values_dict: Mapping[tuple, float],
        base_values_dict: Mapping[tuple, float],
        price_name: str,
        qty_name: str,
    ) -> float:
        return float(
            sum(self._get_var(base_values_dict, price_name, sector) * self._get_var(values_dict, qty_name, sector) for sector in self.sectors)
        )

    def _household_utility(self, values_dict: Mapping[tuple, float], shares: Mapping[str, float]) -> float:
        return math.exp(
            sum(shares[sector] * math.log(max(self._get_var(values_dict, "C", sector), EPSILON)) for sector in self.sectors)
        )

    def _cobb_douglas_expenditure(self, price_dict: Mapping[str, float], utility_level: float, shares: Mapping[str, float]) -> float:
        return utility_level * math.exp(
            sum(shares[sector] * math.log(max(price_dict[sector] / shares[sector], EPSILON)) for sector in self.sectors)
        )

    def _price_index(
        self,
        values_dict: Mapping[tuple, float],
        base_values_dict: Mapping[tuple, float],
        price_name: str,
        shares: Mapping[str, float],
    ) -> float:
        return 100.0 * math.exp(
            sum(
                shares[sector]
                * math.log(
                    max(
                        self._get_var(values_dict, price_name, sector)
                        / self._get_var(base_values_dict, price_name, sector),
                        EPSILON,
                    )
                )
                for sector in self.sectors
            )
        )

    def compute_macro_indicators(
        self,
        values_dict: Mapping[tuple, float],
        base_values_dict: Optional[Mapping[tuple, float]] = None,
    ) -> pd.Series:
        
        shares = self._budget_shares()
        if base_values_dict is None:
            base_values_dict = values_dict

        household_consumption = self._price_quantity_sum(values_dict, "pch", "C")
        government_consumption = self._price_quantity_sum(values_dict, "pcg", "G")
        investment = self._price_quantity_sum(values_dict, "pci", "INV")
        exports = self._price_quantity_sum(values_dict, "pe", "E")
        imports = self._price_quantity_sum(values_dict, "pm", "M")
        total_output_value = self._price_quantity_sum(values_dict, "pz", "Z")
        trade_balance = exports - imports
        coverage_ratio = exports / imports if abs(imports) > EPSILON else np.nan
        total_employment = float(sum(self._get_var(values_dict, "L", sector) for sector in self.sectors))

        nominal_gdp_expenditure = household_consumption + government_consumption + investment + exports - imports
        nominal_gdp_income = self._get_var(values_dict, "Y") + self._get_var(values_dict, "T") - self._get_var(values_dict, "Td")
        real_gdp = (
            self._fixed_price_quantity_sum(values_dict, base_values_dict, "pch", "C")
            + self._fixed_price_quantity_sum(values_dict, base_values_dict, "pcg", "G")
            + self._fixed_price_quantity_sum(values_dict, base_values_dict, "pci", "INV")
            + self._fixed_price_quantity_sum(values_dict, base_values_dict, "pe", "E")
            - self._fixed_price_quantity_sum(values_dict, base_values_dict, "pm", "M")
        )
        gdp_deflator = (nominal_gdp_expenditure / real_gdp) * 100.0 if abs(real_gdp) > EPSILON else np.nan
        cpi = self._price_index(values_dict, base_values_dict, "pch", shares)

        base_price_dict = {sector: self._get_var(base_values_dict, "pch", sector) for sector in self.sectors}
        current_price_dict = {sector: self._get_var(values_dict, "pch", sector) for sector in self.sectors}
        base_utility = self._household_utility(base_values_dict, shares)
        current_utility = self._household_utility(values_dict, shares)
        equivalent_variation = self._cobb_douglas_expenditure(base_price_dict, current_utility, shares) - self._cobb_douglas_expenditure(
            base_price_dict, base_utility, shares
        )
        compensating_variation = self._cobb_douglas_expenditure(current_price_dict, current_utility, shares) - self._cobb_douglas_expenditure(
            current_price_dict, base_utility, shares
        )

        return pd.Series(
            {
                "nominal_gdp_expenditure": nominal_gdp_expenditure,
                "nominal_gdp_income": nominal_gdp_income,
                "real_gdp": real_gdp,
                "gdp_deflator": gdp_deflator,
                "cpi": cpi,
                "household_utility": current_utility,
                "equivalent_variation": equivalent_variation,
                "compensating_variation": compensating_variation,
                "household_income": self._get_var(values_dict, "Y"),
                "disposable_income": self._get_var(values_dict, "Yd"),
                "total_tax_revenue": self._get_var(values_dict, "T"),
                "household_consumption": household_consumption,
                "government_consumption": government_consumption,
                "aggregate_consumption": household_consumption + government_consumption,
                "investment": investment,
                "exports": exports,
                "imports": imports,
                "trade_balance": trade_balance,
                "coverage_ratio": coverage_ratio,
                "total_output_value": total_output_value,
                "total_employment": total_employment,
                "wage_rate": self._get_var(values_dict, "w"),
                "capital_return": self._get_var(values_dict, "r"),
                "private_savings": self._get_var(values_dict, "Sp"),
                "government_savings": self._get_var(values_dict, "Sg"),
                "foreign_savings_local_currency": self._get_var(values_dict, "Sf") * value(self.model.epsilon),
            }
        )

    def compare_macro_results(self, baseline: pd.Series, scenario: pd.Series) -> pd.DataFrame:
        comparison = pd.DataFrame({"baseline": baseline, "scenario": scenario})
        comparison["abs_change"] = comparison["scenario"] - comparison["baseline"]
        comparison["pct_change"] = np.where(
            np.abs(comparison["baseline"]) > EPSILON,
            (comparison["abs_change"] / comparison["baseline"]) * 100.0,
            np.nan,
        )
        return comparison

    def sector_output_changes(
        self,
        baseline_values: Mapping[tuple, float],
        scenario_values: Mapping[tuple, float],
    ) -> pd.DataFrame:
        baseline_output = pd.Series(
            {sector: self._get_var(baseline_values, "Z", sector) for sector in self.sectors},
            name="baseline_output",
        )
        scenario_output = pd.Series(
            {sector: self._get_var(scenario_values, "Z", sector) for sector in self.sectors},
            name="scenario_output",
        )
        output = pd.concat([baseline_output, scenario_output], axis=1)
        output["abs_change"] = output["scenario_output"] - output["baseline_output"]
        output["pct_change"] = np.where(
            np.abs(output["baseline_output"]) > EPSILON,
            (output["abs_change"] / output["baseline_output"]) * 100.0,
            np.nan,
        )
        return output.sort_values("pct_change", ascending=False)

    def spillover_effect(
        self,
        shocked_sectors: Sequence[str],
        baseline_values: Mapping[tuple, float],
        scenario_values: Mapping[tuple, float],
    ) -> pd.Series:
        output_changes = self.sector_output_changes(baseline_values, scenario_values)
        non_shocked_sectors = [sector for sector in self.sectors if sector not in set(shocked_sectors)]
        if not non_shocked_sectors:
            return pd.Series(
                {
                    "spillover_output_change": 0.0,
                    "spillover_output_pct_change": 0.0,
                }
            )

        spillover_output_change = float(output_changes.loc[non_shocked_sectors, "abs_change"].sum())
        non_shocked_baseline_output = float(output_changes.loc[non_shocked_sectors, "baseline_output"].sum())
        spillover_output_pct_change = _safe_ratio(spillover_output_change, non_shocked_baseline_output) * 100.0
        return pd.Series(
            {
                "spillover_output_change": spillover_output_change,
                "spillover_output_pct_change": spillover_output_pct_change,
            }
        )

    def solve_baseline(self, tee: bool = False) -> ScenarioResult:
        if self.model is None:
            self.build_model()
        self.reset_productivity()
        results, elapsed = self.solve(tee=tee)
        values_dict = self.capture_var_values(self.model)
        macro = self.compute_macro_indicators(values_dict, base_values_dict=values_dict)
        comparison = self.compare_macro_results(macro, macro)
        output_changes = self.sector_output_changes(values_dict, values_dict)

        scenario = ScenarioResult(
            name="baseline",
            shocked_sectors=[],
            shock_size=0.0,
            solve_time_seconds=elapsed,
            solver_status=str(results.solver.status),
            termination_condition=str(results.solver.termination_condition),
            solution_values=values_dict,
            macro_results=macro,
            macro_comparison=comparison,
            macro_changes=comparison["pct_change"],
            sector_output_changes=output_changes,
        )

        self.baseline_result = scenario
        return scenario

    def run_scenario(
        self,
        shocked_sectors: Sequence[str],
        shock_size: float = 0.10,
        name: str = "scenario",
        tee: bool = False,
    ) -> ScenarioResult:
        
        if self.baseline_result is None:
            self.solve_baseline(tee=tee)
        assert self.baseline_result is not None
        if self.model is None:
            self.build_model()
        assert self.model is not None

        self._load_solution_as_start(self.baseline_result.solution_values)
        self.apply_productivity_shock(shocked_sectors, shock_size=shock_size)

        results, elapsed = self.solve(tee=tee)
        values_dict = self.capture_var_values(self.model)
        macro = self.compute_macro_indicators(values_dict, base_values_dict=self.baseline_result.solution_values)
        comparison = self.compare_macro_results(self.baseline_result.macro_results, macro)
        equivalent_variation_share = _safe_ratio(
            macro["equivalent_variation"], self.baseline_result.macro_results["household_income"]
        ) * 100.0
        compensating_variation_share = _safe_ratio(
            macro["compensating_variation"], self.baseline_result.macro_results["household_income"]
        ) * 100.0
        welfare_change_share = 0.5 * (equivalent_variation_share + compensating_variation_share)

        comparison.loc["equivalent_variation_share_of_income", "baseline"] = 0.0
        comparison.loc["equivalent_variation_share_of_income", "scenario"] = equivalent_variation_share
        comparison.loc["equivalent_variation_share_of_income", "abs_change"] = comparison.loc[
            "equivalent_variation_share_of_income", "scenario"
        ]
        comparison.loc["equivalent_variation_share_of_income", "pct_change"] = np.nan

        comparison.loc["compensating_variation_share_of_income", "baseline"] = 0.0
        comparison.loc["compensating_variation_share_of_income", "scenario"] = compensating_variation_share
        comparison.loc["compensating_variation_share_of_income", "abs_change"] = comparison.loc[
            "compensating_variation_share_of_income", "scenario"
        ]
        comparison.loc["compensating_variation_share_of_income", "pct_change"] = np.nan

        comparison.loc["welfare_change_share_of_income", "baseline"] = 0.0
        comparison.loc["welfare_change_share_of_income", "scenario"] = welfare_change_share
        comparison.loc["welfare_change_share_of_income", "abs_change"] = comparison.loc[
            "welfare_change_share_of_income", "scenario"
        ]
        comparison.loc["welfare_change_share_of_income", "pct_change"] = np.nan

        comparison.loc["net_external_balance_change", "baseline"] = 0.0
        comparison.loc["net_external_balance_change", "scenario"] = comparison.loc["trade_balance", "abs_change"]
        comparison.loc["net_external_balance_change", "abs_change"] = comparison.loc[
            "net_external_balance_change", "scenario"
        ]
        comparison.loc["net_external_balance_change", "pct_change"] = np.nan

        spillover = self.spillover_effect(
            shocked_sectors=shocked_sectors,
            baseline_values=self.baseline_result.solution_values,
            scenario_values=values_dict,
        )
        for metric in spillover.index:
            comparison.loc[metric, "baseline"] = 0.0
            comparison.loc[metric, "scenario"] = spillover[metric]
            comparison.loc[metric, "abs_change"] = spillover[metric]
            comparison.loc[metric, "pct_change"] = np.nan

        output_changes = self.sector_output_changes(self.baseline_result.solution_values, values_dict)
        macro_changes = comparison["pct_change"].copy()
        macro_changes.loc["trade_balance_change"] = comparison.loc["trade_balance", "abs_change"]
        macro_changes.loc["equivalent_variation_share_of_income"] = comparison.loc[
            "equivalent_variation_share_of_income", "scenario"
        ]
        macro_changes.loc["compensating_variation_share_of_income"] = comparison.loc[
            "compensating_variation_share_of_income", "scenario"
        ]
        macro_changes.loc["welfare_change_share_of_income"] = comparison.loc["welfare_change_share_of_income", "scenario"]
        macro_changes.loc["net_external_balance_change"] = comparison.loc["net_external_balance_change", "scenario"]
        macro_changes.loc["spillover_output_change"] = comparison.loc["spillover_output_change", "scenario"]
        macro_changes.loc["spillover_output_pct_change"] = comparison.loc["spillover_output_pct_change", "scenario"]

        return ScenarioResult(
            name=name,
            shocked_sectors=list(shocked_sectors),
            shock_size=shock_size,
            solve_time_seconds=elapsed,
            solver_status=str(results.solver.status),
            termination_condition=str(results.solver.termination_condition),
            solution_values=values_dict,
            macro_results=macro,
            macro_comparison=comparison,
            macro_changes=macro_changes,
            sector_output_changes=output_changes,
        )

    def scenario_change_table(self, scenarios: Sequence[ScenarioResult]) -> pd.DataFrame:
        records = []
        for scenario in scenarios:
            comparison = scenario.macro_comparison

            def _metric_value(metric: str, column: str, default: float = np.nan) -> float:
                if metric in comparison.index and column in comparison.columns:
                    return float(comparison.loc[metric, column])
                return float(default)

            record = {
                "scenario": scenario.name,
                "shocked_sectors": ",".join(scenario.shocked_sectors),
                "shock_size": scenario.shock_size,
                "solve_time_seconds": scenario.solve_time_seconds,
                "trade_balance_change": _metric_value("trade_balance", "abs_change"),
                "net_external_balance_change": _metric_value("net_external_balance_change", "scenario"),
                "equivalent_variation_share_of_income": _metric_value("equivalent_variation_share_of_income", "scenario"),
                "compensating_variation_share_of_income": _metric_value(
                    "compensating_variation_share_of_income", "scenario"
                ),
                "welfare_change_share_of_income": _metric_value("welfare_change_share_of_income", "scenario"),
                "spillover_output_change": _metric_value("spillover_output_change", "scenario"),
                "spillover_output_pct_change": _metric_value("spillover_output_pct_change", "scenario"),
            }
            for metric, row in comparison.iterrows():
                if metric in {
                    "equivalent_variation_share_of_income",
                    "compensating_variation_share_of_income",
                    "welfare_change_share_of_income",
                    "net_external_balance_change",
                    "spillover_output_change",
                    "spillover_output_pct_change",
                }:
                    continue
                record[f"{metric}_pct_change"] = row["pct_change"]
            records.append(record)
        return pd.DataFrame(records).set_index("scenario")

    def calibrate(self):
        """Public calibration hook; calibration is performed during initialization."""
        self.calibration = self._calibrate_from_sam()
        self.sectors = self.calibration.sectors
        self.baseline_output = pd.Series(self.calibration.benchmark["z0"], name="baseline_output")
        return self.calibration

    def solve_model(self, tee: bool = False):
        """Solve the current model state and return macro indicators plus diagnostics."""
        if self.model is None:
            self.build_model()
        results, elapsed = self.solve(tee=tee)
        values_dict = self.capture_var_values(self.model)
        base_values = self.baseline_result.solution_values if self.baseline_result is not None else values_dict
        macro = self.compute_macro_indicators(values_dict, base_values_dict=base_values)
        return {
            "macro": macro,
            "solution_values": values_dict,
            "solver_status": str(results.solver.status),
            "termination_condition": str(results.solver.termination_condition),
            "solve_time_seconds": elapsed,
            "walras_residual": self.walras_residual(values_dict),
        }

    def shock_model(self, shock_size=0.10):
        """Reset productivity and apply scalar or sector-specific productivity shocks."""
        if self.model is None:
            self.build_model()
        self.reset_productivity()
        base_aprod = self.calibration.params["aprod"]
        if isinstance(shock_size, Mapping):
            for sector in self.sectors:
                shock = float(shock_size.get(sector, 0.0))
                self.model.Aprod[sector].set_value(base_aprod[sector] * (1.0 + shock))
        else:
            for sector in self.sectors:
                self.model.Aprod[sector].set_value(base_aprod[sector] * (1.0 + float(shock_size)))
        return self.model

    def walras_residual(self, values_dict: Mapping[tuple, float]) -> float:
        """Residual of the commodity market equation dropped by Walras's law."""
        sector = self.sectors[-1]
        lhs = self._get_var(values_dict, "Q", sector)
        rhs = (
            self._get_var(values_dict, "C", sector)
            + self._get_var(values_dict, "G", sector)
            + self._get_var(values_dict, "INV", sector)
            + sum(self._get_var(values_dict, "I", (sector, user)) for user in self.sectors)
        )
        return float(lhs - rhs)

    def baseline_validation(self):
        """Compare solved baseline quantities/prices against calibrated SAM benchmark."""
        if self.baseline_result is None:
            self.solve_baseline()
        assert self.baseline_result is not None
        values = self.baseline_result.solution_values
        base = self.calibration.benchmark
        checks = {}
        for sector in self.sectors:
            for var_name, bench_key in [("Z", "z0"), ("X", "x0"), ("E", "e0"), ("M", "m0"), ("C", "c0"), ("G", "g0"), ("INV", "inv0")]:
                checks[(var_name, sector)] = self._get_var(values, var_name, sector) - base[bench_key][sector]
        return pd.Series(checks, name="baseline_deviation")

    def _functional_labor_share(self, macro: pd.Series) -> float:
        lbar = self.calibration.params["lbar"]
        kbar = self.calibration.params["kbar"]
        w = float(macro["wage_rate"])
        r = float(macro["capital_return"])
        return _safe_ratio(w * lbar, w * lbar + r * kbar)

    def counterfactual_result(self, shock_size=0.10, tee: bool = False):
        """Run one-at-a-time sector productivity shocks and return EIPI indicators."""
        baseline = self.solve_baseline(tee=tee)
        rows = []
        base_macro = baseline.macro_results.copy()
        base_labor_share = self._functional_labor_share(base_macro)
        base_output = pd.Series({s: self._get_var(baseline.solution_values, "Z", s) for s in self.sectors}, name="baseline_output")
        for sector in self.sectors:
            scenario_shock = float(shock_size.get(sector, 0.0)) if isinstance(shock_size, Mapping) else float(shock_size)
            scenario = self.run_scenario([sector], shock_size=scenario_shock, name=sector, tee=tee)
            macro = scenario.macro_results
            cf_labor_share = self._functional_labor_share(macro)
            absorption_base = base_macro["total_output_value"] + base_macro["imports"] - base_macro["exports"]
            absorption_cf = macro["total_output_value"] + macro["imports"] - macro["exports"]
            import_dep_base = _safe_ratio(base_macro["imports"], absorption_base)
            import_dep_cf = _safe_ratio(macro["imports"], absorption_cf)
            export_share_base = _safe_ratio(base_macro["exports"], base_macro["real_gdp"])
            export_share_cf = _safe_ratio(macro["exports"], macro["real_gdp"])
            row = {
                "shock_size": scenario_shock,
                "baseline_output": base_output[sector],
                "output_share": _safe_ratio(base_output[sector], base_output.sum()),
                "solver_status": scenario.solver_status,
                "termination_condition": scenario.termination_condition,
                "walras_residual": self.walras_residual(scenario.solution_values),
                "GDP_base": base_macro["real_gdp"],
                "GDP_counterfactual": macro["real_gdp"],
                "GDP_pct_change": _safe_ratio(macro["real_gdp"] - base_macro["real_gdp"], base_macro["real_gdp"]) * 100.0,
                "Welfare_base": base_macro["household_utility"],
                "Welfare_counterfactual": macro["household_utility"],
                "Welfare_pct_change": _safe_ratio(macro["household_utility"] - base_macro["household_utility"], base_macro["household_utility"]) * 100.0,
                "ExportShare_base": export_share_base,
                "ExportShare_counterfactual": export_share_cf,
                "ExportShare_pct_change": _safe_ratio(export_share_cf - export_share_base, export_share_base) * 100.0,
                "LaborIncomeShare_base": base_labor_share,
                "LaborIncomeShare_counterfactual": cf_labor_share,
                "LaborIncomeShare_pct_change": _safe_ratio(cf_labor_share - base_labor_share, base_labor_share) * 100.0,
                "Spillover_base": 0.0,
                "Spillover_counterfactual": scenario.macro_changes["spillover_output_pct_change"],
                "Spillover_pct_change": scenario.macro_changes["spillover_output_pct_change"],
                "ImportDependency_base": import_dep_base,
                "ImportDependency_counterfactual": import_dep_cf,
                "ImportDependency_pct_change": _safe_ratio(import_dep_cf - import_dep_base, import_dep_base) * 100.0,
                "CPI_base": base_macro["cpi"],
                "CPI_counterfactual": macro["cpi"],
                "CPI_pct_change": _safe_ratio(macro["cpi"] - base_macro["cpi"], base_macro["cpi"]) * 100.0,
            }
            rows.append((sector, row))
        out = pd.DataFrame(dict(rows)).T
        self.baseline_output = base_output
        return out

    def size_normalized_shock_map(self, base_shock=0.10):
        if not hasattr(self, "baseline_output"):
            if self.baseline_result is None:
                self.solve_baseline()
            self.baseline_output = pd.Series({s: self._get_var(self.baseline_result.solution_values, "Z", s) for s in self.sectors}, name="baseline_output")
        shares = self.baseline_output / self.baseline_output.sum()
        mean_share = float(shares.mean())
        return {s: min(max(base_shock * mean_share / max(float(shares.loc[s]), EPSILON), 0.005), 0.50) for s in self.sectors}
