"""Restoration-time estimate: STORMS labor hours + Monte Carlo + upstream-first step restoration.

No ETA model is trained (ARCOS dates are day-level). Instead:
  damage on the premise's circuit/path (from asset_risk probabilities)
  x crew-hours per repair job (CU bundles from data/structure_drawings.pdf, STORMS hours, ILM)
  / crews + arrival delay, sequenced upstream-first.

Device IDs never leave this module; the bottleneck is described with a friendly name.
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

N_RUNS = 2000
HOURS_SIGMA = 0.35  # lognormal spread of each job's crew-hours around its bundle mean
SEVERITIES = ("low", "medium", "high")

REPLACE, INSTALL = "replace", "install"  # replace = removal + install hours; install = install only

# Repair-job bundles. Each variant: (probability, description, [(STORMS CU group, count, mode)]).
# Assemblies and D.S. numbers are from AEP distribution standards in data/structure_drawings.pdf.
BUNDLES = {
    # Pole itself is added separately, sized from the GIS pole height. Framing is re-installed on the new pole;
    # the GIS has no framing type, so the structure mix is an assumption.
    "pole": [
        (0.5, "single-phase tangent (p.5: pin & insulator D.S. 30, neutral assembly D.S. 500)",
         [("Pin", 1, REPLACE), ("Insulator", 1, REPLACE), ("Secondary Conductor Attachment", 1, REPLACE)]),
        (0.3, "three-phase crossarm tangent (p.9: crossarm D.S. 26, pins/insulators D.S. 30-A/B, neutral D.S. 500)",
         [("Crossarm", 1, REPLACE), ("Pin", 3, REPLACE), ("Insulator", 3, REPLACE),
          ("Secondary Conductor Attachment", 1, REPLACE)]),
        (0.2, "three-phase dead-end (p.3, p.8: crossarm D.S. 20, primary dead-ends D.S. 31, neutral dead-end "
              "D.S. 501, guying D.S. 400, connector D.S. 70, grounding D.S. 65)",
         [("Crossarm", 1, REPLACE), ("Dead-End", 4, REPLACE), ("Guying", 1, REPLACE), ("Anchor", 1, REPLACE),
          ("Connector", 1, INSTALL), ("Grounding", 1, REPLACE)]),
    ],
    # STORMS has no conductor-stringing CU, so wire jobs are a lower bound (splices + re-tie only).
    "primary_wire": [
        (0.7, "single-phase splice/re-terminate (connectors D.S. 70, pin insulator D.S. 30)",
         [("Connector", 3, INSTALL), ("Insulator", 1, REPLACE)]),
        (0.3, "three-phase splice/re-terminate (connectors D.S. 70, pin insulators D.S. 30)",
         [("Connector", 7, INSTALL), ("Insulator", 3, REPLACE)]),
    ],
    "secondary_wire": [
        (1.0, "twisted-cable secondary (p.1-2: D.S. 502, connectors D.S. 70)",
         [("Secondary Conductor Attachment", 2, REPLACE), ("Connector", 2, INSTALL)]),
    ],
    "service": [(1.0, "service drop to the home", [("Service", 1, REPLACE)])],
    "fuse": [
        (1.0, "cutout + fuse link (p.1-2: D.S. 61, D.S. 1001, bracket D.S. 32-E, hot-line tap D.S. 75-A, lead)",
         [("Fuse Cutout", 1, REPLACE), ("Bracket", 1, REPLACE), ("Connector", 1, INSTALL),
          ("Equipment Lead", 1, REPLACE)]),
    ],
    # Transformer CU is added separately, sized by kVA from the GIS.
    "transformer": [
        (1.0, "single-phase pole transformer (p.2: D.S. 1000, cutout D.S. 61, brackets D.S. 32-E/32-C, "
              "leads D.S. 75-A, connectors D.S. 70, riser D.S. 1002, grounding D.S. 65)",
         [("Fuse Cutout", 1, REPLACE), ("Bracket", 2, REPLACE), ("Equipment Lead", 2, REPLACE),
          ("Connector", 2, INSTALL), ("Secondary Conductor Attachment", 1, REPLACE), ("Grounding", 1, REPLACE)]),
    ],
    # No drawing for these: assumed device + leads + connectors + bracket + grounding.
    "recloser": [(1.0, "recloser (no drawing; assumed)",
                  [("Recloser", 1, REPLACE), ("Equipment Lead", 3, REPLACE), ("Connector", 3, INSTALL),
                   ("Bracket", 1, REPLACE), ("Grounding", 1, REPLACE)])],
    "switch": [(1.0, "switch (no drawing; assumed)",
                [("Switch", 1, REPLACE), ("Equipment Lead", 2, REPLACE), ("Connector", 2, INSTALL),
                 ("Bracket", 1, REPLACE), ("Grounding", 1, REPLACE)])],
}

# asset_risk.asset_type -> job type, ARCOS type (for the duration multiplier), friendly plural.
ASSET_JOB = {
    "pole": ("pole", "Pole", "pole replacements"),
    "primary": ("primary_wire", "primary", "power-line repairs"),
    "secondary": ("secondary_wire", "secondary", "street-level wire repairs"),
    "fuse": ("fuse", "Fuse Bank", "fuse repairs"),
    "transformer": ("transformer", "Transformer", "transformer replacements"),
    "recloser": ("recloser", "Recloser Bank", "recloser repairs"),
    "switch": ("switch", "Switch", "switch repairs"),
}
JOB_LABEL = {
    "pole": "pole replacements", "primary_wire": "power-line repairs", "secondary_wire": "street-level wire repairs",
    "service": "service-line repairs", "fuse": "fuse repairs", "transformer": "transformer replacements",
    "recloser": "recloser repairs", "switch": "switch repairs",
}
# What-if / confirmed damage keys accepted from callers (case-insensitive) -> job type.
DAMAGE_KEYS = {
    "pole": "pole", "poles": "pole", "wire": "primary_wire", "primary": "primary_wire", "line": "primary_wire",
    "secondary": "secondary_wire", "service": "service", "fuse": "fuse", "fuse bank": "fuse",
    "transformer": "transformer", "recloser": "recloser", "recloser bank": "recloser", "switch": "switch",
}
# Upstream-first priority: 1 = main line/protective devices, 2 = branch lines, 3 = street level/service.
JOB_TIER = {"recloser": 1, "switch": 1, "fuse": 2, "pole": 2, "primary_wire": 2,
            "transformer": 3, "secondary_wire": 3, "service": 3}
DEVICE_NAMES = {
    "Distribution": "the transformer that serves your street",
    "Fuse": "the fuse protecting your branch line",
    "Recloser (Hydraulic)": "the hydraulic recloser on your line",
    "Recloser (Electronic)": "the electronic recloser on your line",
    "Sectionalizer": "the sectionalizer on your line",
    "Step": "the step-down transformer on your line",
    "Circuit Breaker": "the circuit breaker at your substation",
}


def pole_size(height: str | None) -> str:
    try:
        ft = float(str(height).split()[0])
    except (TypeError, ValueError):
        return "Medium"
    return "Small" if ft < 40 else "Medium" if ft < 60 else "Large"


def kva_size(spec: str | None, sizes: list[float]) -> str:
    try:
        kva = float(str(spec).split()[0])
    except (TypeError, ValueError):
        kva = 25.0
    return f"{min(sizes, key=lambda s: abs(s - kva)):g}"


@dataclass
class LaborModel:
    """STORMS labor hours (per CU group), indirect labor multiplier and access adders, from Postgres."""
    cu_hours: dict  # (cu_group, size_label|None) -> (install, removal)
    ilm: float
    access_adder_hours: float
    transformer_sizes: list = field(default_factory=list)

    @classmethod
    def from_db(cls, engine: Engine) -> "LaborModel":
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT cu_group, size_label, avg_install_hours, avg_removal_hours FROM labor_cu_group_avg")).all()
            ilm = conn.execute(text("SELECT value FROM labor_indirect_multiplier LIMIT 1")).scalar()
            adders = dict(conn.execute(text("SELECT original_intent, install_hours FROM labor_adders")).all())
        cu = {(g, s): (float(i or 0), float(r or 0)) for g, s, i, r in rows}
        sizes = sorted(float(s) for g, s in cu if g == "Transformer" and s)
        access = float(adders.get("Access Constraint Adder") or 0) + float(adders.get("Remote Location Adder") or 0)
        return cls(cu, float(ilm), access, sizes)

    def _cu(self, group: str, count: int, mode: str, size: str | None = None) -> float:
        install, removal = self.cu_hours[(group, size)]
        return count * (install + (removal if mode == REPLACE else 0.0))

    def variants(self, job: str, size: str | None = None) -> list[tuple[float, float]]:
        """[(probability, mean crew-hours incl. ILM)] for a job type."""
        base = 0.0
        if job == "pole":
            base = self._cu("Pole", 1, REPLACE, size or "Medium")
        elif job == "transformer":
            base = self._cu("Transformer", 1, REPLACE, size or "25")
        return [(p, (base + sum(self._cu(g, n, m) for g, n, m in parts)) * self.ilm) for p, _, parts in BUNDLES[job]]

    def mean_hours(self, job: str, size: str | None = None) -> float:
        return sum(p * h for p, h in self.variants(job, size))


@dataclass
class Scenario:
    storm_severity: str = "medium"
    crews: int = 2
    crew_arrival_delay_hrs: float = 0.0
    extra_damage: dict = field(default_factory=dict)
    access_blocked: bool = False
    storm_n: float | None = None  # explicit storm size (e.g. from the forecast); overrides storm_severity

    @classmethod
    def from_dict(cls, d: dict | None) -> "Scenario":
        d = dict(d or {})
        sev = {"med": "medium"}.get(d.get("storm_severity"), d.get("storm_severity", "medium"))
        if sev not in SEVERITIES:
            raise ValueError(f"storm_severity must be one of {SEVERITIES}")
        crews = int(d.get("crews", 2))
        if crews < 1:
            raise ValueError("crews must be at least 1")
        delay = float(d.get("crew_arrival_delay_hrs", 0.0))
        if delay < 0:
            raise ValueError("crew_arrival_delay_hrs cannot be negative")
        extra = {}
        for k, v in (d.get("extra_damage") or {}).items():
            job = DAMAGE_KEYS.get(str(k).strip().lower())
            if job is None:
                raise ValueError(f"unknown damage type '{k}'; use one of {sorted(set(DAMAGE_KEYS))}")
            extra[job] = extra.get(job, 0) + max(0, int(v))
        storm_n = d.get("storm_n")
        if storm_n is not None and float(storm_n) < 0:
            raise ValueError("storm_n cannot be negative")
        return cls(sev, crews, delay, extra, bool(d.get("access_blocked", False)),
                   None if storm_n is None else float(storm_n))

    def as_dict(self) -> dict:
        return {"storm_severity": self.storm_severity, "crews": self.crews,
                "crew_arrival_delay_hrs": self.crew_arrival_delay_hrs, "extra_damage": dict(self.extra_damage),
                "access_blocked": self.access_blocked, "storm_n": self.storm_n}


class RestorationEstimator:
    def __init__(self, engine: Engine, scorer, labor: LaborModel | None = None):
        self.engine = engine
        self.scorer = scorer
        self.labor = labor or LaborModel.from_db(engine)
        with engine.connect() as conn:
            self.pole_size = {f"pole:{i}": pole_size(h) for i, h in conn.execute(text("SELECT id, height FROM poles"))}
            self.xfmr_size = {f"transformer:{i}": kva_size(s, self.labor.transformer_sizes)
                              for i, s in conn.execute(text("SELECT id, a_phase_sp FROM transformers"))}
        assets = scorer.assets
        self.circuit_assets = {ck: g for ck, g in assets.groupby("circuit_key")}
        self.access_mult = self._access_multipliers()

    def _access_multipliers(self) -> dict:
        """ARCOS-history slowdown for hard-access repairs, per job type (ml.duration), 1.0 if unavailable."""
        try:
            import joblib

            from ml.duration import BUNDLE_PATH, estimate_repair_days
            bundle = joblib.load(BUNDLE_PATH)
        except Exception:  # duration helper not trained: no history-based slowdown
            return {}
        out = {}
        for job, arcos_type, _ in ASSET_JOB.values():
            slow = estimate_repair_days(arcos_type, True, bundle=bundle)["p50_days"] + 1
            normal = estimate_repair_days(arcos_type, False, bundle=bundle)["p50_days"] + 1
            out[job] = max(1.0, slow / normal)
        out.setdefault("service", out.get("secondary_wire", 1.0))
        return out

    # ---- public --------------------------------------------------------------------------------

    def estimate(self, premise: str, scenario: dict | Scenario | None = None, confirmed: dict | None = None,
                 seed: int = 0, n_runs: int = N_RUNS, area: tuple | None = None) -> dict:
        sc = scenario if isinstance(scenario, Scenario) else Scenario.from_dict(scenario)
        premise = str(premise).strip()
        if premise not in self.scorer.customers.index:
            return self._fallback(premise, sc, "We couldn't find this service location in our records.")
        cust = self.scorer.customers.loc[premise]
        in_topology = self.scorer.graph.has_node(("premise", premise))
        parts = self.scorer.exposure(premise, cust, in_topology)
        exposure = pd.concat([df.assign(component=k) for k, df in parts.items()], ignore_index=True)
        if exposure.empty:
            return self._fallback(premise, sc, "There isn't enough network information near this location.")

        storm_n = self.scorer.storm_size(sc.storm_severity, sc.storm_n)
        jobs = self._job_table(exposure, cust["circuit_key"], storm_n, area)
        confirmed_jobs = {DAMAGE_KEYS.get(str(k).lower(), k): int(v) for k, v in (confirmed or {}).items()}
        sim = self._simulate(jobs, sc, confirmed_jobs, seed, n_runs)
        hops = self._upline_hops(premise)
        return self._result(premise, sc, jobs, sim, hops, confirmed_jobs, in_topology)

    # ---- internals ----------------------------------------------------------------------------

    def _job_table(self, exposure: pd.DataFrame, circuit_key, storm_n: float, area: tuple | None = None) -> pd.DataFrame:
        """All assets that can hold up this premise: its exposure set + the rest of its circuit."""
        circuit = self.circuit_assets.get(circuit_key, exposure.iloc[0:0])
        circuit = circuit[~circuit["asset_uid"].isin(exposure["asset_uid"])].assign(component="circuit")
        t = pd.concat([exposure, circuit], ignore_index=True)
        t["p"] = self.scorer.p(t, storm_n, area)
        t["job"] = t["asset_type"].map(lambda a: ASSET_JOB[a][0])
        # The premise's own secondary segment is its service drop.
        own_secondary = (t["component"] == "your_equipment") & (t["asset_type"] == "secondary")
        t.loc[own_secondary, "job"] = "service"
        t["tier"] = t["job"].map(JOB_TIER)
        t.loc[(t["asset_type"] == "primary") & (t["line_usage"] == "Backbone"), "tier"] = 1
        t["on_path"] = t["component"] != "circuit"
        t["size"] = [self.pole_size.get(u) if j == "pole" else self.xfmr_size.get(u) if j == "transformer" else None
                     for u, j in zip(t["asset_uid"], t["job"])]
        return t

    def _sample_hours(self, rng, jobs: list[str], sizes: list, n: int, access: np.ndarray) -> np.ndarray:
        """Crew-hours for n damaged jobs: pick a bundle variant, then lognormal around its mean."""
        out = np.empty(n)
        keys = pd.Series(list(zip(jobs, sizes)), dtype=object)
        for (job, size), idx in keys.groupby(keys).groups.items():
            v = self.labor.variants(job, size)
            probs, means = np.array([p for p, _ in v]), np.array([h for _, h in v])
            out[idx] = means[rng.choice(len(means), size=len(idx), p=probs)]
        out = out * rng.lognormal(-HOURS_SIGMA ** 2 / 2, HOURS_SIGMA, n)  # mean-preserving noise
        if access.any():
            mult = np.array([self.access_mult.get(j, 1.0) for j in jobs])
            out = np.where(access, (out + self.labor.access_adder_hours * self.labor.ilm) * mult, out)
        return out

    def _simulate(self, t: pd.DataFrame, sc: Scenario, confirmed: dict, seed: int, n_runs: int) -> dict:
        rng = np.random.default_rng(seed)
        n = len(t)
        p = t["p"].to_numpy()
        on_path = t["on_path"].to_numpy()
        tiers = t["tier"].to_numpy()

        damaged = rng.random((n_runs, n)) < p
        if confirmed:  # assessed damage replaces sampled damage on this premise's path
            damaged[:, on_path] = False
        else:  # condition on the premise being out: force one path failure where none was sampled
            no_out = ~damaged[:, on_path].any(axis=1)
            path_idx = np.flatnonzero(on_path)
            w = p[path_idx] / p[path_idx].sum() if p[path_idx].sum() > 0 else None
            forced = rng.choice(path_idx, size=no_out.sum(), p=w)
            damaged[np.flatnonzero(no_out), forced] = True

        # Extra (what-if) and confirmed jobs are added on the premise's path in every run.
        extra = {}
        for src in (sc.extra_damage, confirmed):
            for job, k in src.items():
                extra[job] = extra.get(job, 0) + k

        run_idx, col_idx = np.nonzero(damaged)
        jobs = t["job"].to_numpy()[col_idx].tolist()
        sizes = t["size"].to_numpy()[col_idx].tolist()
        access = on_path[col_idx] & sc.access_blocked
        hours = self._sample_hours(rng, jobs, sizes, len(col_idx), access)
        job_tier = tiers[col_idx]
        job_path = on_path[col_idx]
        for job, k in extra.items():
            r = np.repeat(np.arange(n_runs), k)
            run_idx = np.concatenate([run_idx, r])
            jobs += [job] * len(r)
            hours = np.concatenate([hours, self._sample_hours(rng, [job] * len(r), [None] * len(r), len(r),
                                                              np.full(len(r), sc.access_blocked))])
            job_tier = np.concatenate([job_tier, np.full(len(r), JOB_TIER[job])])
            job_path = np.concatenate([job_path, np.ones(len(r), bool)])

        # Work per run per tier: all circuit work, and the premise's own path work.
        work = np.zeros((n_runs, 4))
        own = np.zeros((n_runs, 4))
        np.add.at(work, (run_idx, job_tier), hours)
        np.add.at(own, (run_idx[job_path], job_tier[job_path]), hours[job_path])
        own_tiers = np.where(own > 0, np.arange(4), 0)
        deepest = own_tiers.max(axis=1)  # deepest tier with damage on the premise's path

        # Upstream-first: all circuit work above the deepest tier, the premise's own work at that tier,
        # and a random share of other same-tier work (crews may reach other branches first).
        share = rng.random(n_runs)
        runs = np.arange(n_runs)
        cum = work.cumsum(axis=1)  # work[:, 0] is always 0
        above = np.where(deepest > 0, cum[runs, np.maximum(deepest - 1, 0)], 0.0)
        at_own = own[runs, deepest]
        at_other = work[runs, deepest] - at_own
        crew_hours = above + at_own + share * at_other
        total = sc.crew_arrival_delay_hrs + crew_hours / sc.crews

        contrib = np.zeros((n_runs, 4))
        for tier in (1, 2, 3):
            contrib[:, tier] = np.where(tier < deepest, work[:, tier],
                                        np.where(tier == deepest, at_own + share * at_other, 0.0)) / sc.crews

        path_counts = pd.Series(jobs)[job_path].value_counts() / n_runs if job_path.any() else pd.Series(dtype=float)
        circuit_repairs = len(run_idx) / n_runs
        return {"total": total, "contrib": contrib.mean(axis=0), "deepest": deepest,
                "path_counts": path_counts, "circuit_repairs": circuit_repairs}

    def _upline_hops(self, premise: str) -> list[str]:
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT subtype FROM premise_upline WHERE premise_nu = :p ORDER BY hop_seq"),
                                {"p": premise}).scalars().all()

    @staticmethod
    def _bottleneck(tier: int, hops: list[str]) -> str:
        def first(pred, default):
            return next((DEVICE_NAMES.get(h, "a device on your line") for h in hops if pred(h)), default)
        if tier == 1:
            main_line = DEVICE_NAMES["Circuit Breaker"] if "Circuit Breaker" in hops else "the main line serving your area"
            return first(lambda h: h.startswith("Recloser") or h == "Sectionalizer", main_line)
        if tier == 2:
            return first(lambda h: h == "Fuse", "the branch line serving your neighborhood")
        return DEVICE_NAMES["Distribution"]

    def _result(self, premise, sc, t, sim, hops, confirmed, in_topology) -> dict:
        p10, p50, p90 = (round(float(np.percentile(sim["total"], q)) * 2) / 2 for q in (10, 50, 90))
        bottleneck_tier = int(np.argmax(sim["contrib"][1:]) + 1)

        drivers = []
        for job, count in sim["path_counts"].sort_values(ascending=False).items():
            if count >= 0.05:
                drivers.append(f"About {count:.1f} expected {JOB_LABEL[job]} on or near your line")
        drivers.append(f"About {sim['circuit_repairs']:.0f} repairs expected across the circuit that serves you")
        drivers.append(f"{sc.crews} crew{'s' if sc.crews != 1 else ''} assigned to your circuit")
        if sc.crew_arrival_delay_hrs:
            drivers.append(f"Crews expected to arrive after {sc.crew_arrival_delay_hrs:g} hours")
        if sc.access_blocked:
            drivers.append("Blocked access adds time to repairs near you")
        if confirmed:
            drivers.append("Uses damage confirmed by field assessment on your line")

        assumptions = [
            f"Storm size: about {self.scorer.storm_size(sc.storm_severity, sc.storm_n):.0f} damage reports across the "
            "area; damage on your line and circuit is sampled from the outage-risk "
            f"model ({len(sim['total']):,} simulations).",
            "Assumes your power is out; the range is how long until it is back.",
            f"Repair times use AEP STORMS labor hours for each repair's parts (from AEP structure drawings), times "
            f"an indirect-labor multiplier of {self.labor.ilm:g} for travel, safety and setup.",
            "Crews repair the main line first, then branch lines, then street-level equipment.",
            "Wire repairs count splicing and re-tying only (no restringing data), so long wire-down repairs may "
            "take longer.",
        ]
        if not in_topology:
            assumptions.append("Your exact network connection isn't in our data, so nearby equipment is used.")
        return {
            "premise": premise,
            "p10_hrs": p10, "p50_hrs": p50, "p90_hrs": p90,
            "bottleneck_device": self._bottleneck(bottleneck_tier, hops),
            "drivers": drivers,
            "scenario": sc.as_dict(),
            "assumptions": assumptions,
            "label": "MODEL_ESTIMATE",
            "fallback": False,
        }

    @staticmethod
    def _fallback(premise, sc: Scenario, reason: str) -> dict:
        return {
            "premise": premise, "p10_hrs": None, "p50_hrs": None, "p90_hrs": None, "bottleneck_device": None,
            "drivers": [], "scenario": sc.as_dict(),
            "assumptions": ["No reliable restoration estimate is possible for this location."],
            "message": f"{reason} We can't estimate restoration time here; please report your outage so crews "
                       "know about it.",
            "label": "MODEL_ESTIMATE", "fallback": True,
        }
