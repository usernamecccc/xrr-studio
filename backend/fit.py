"""差分进化拟合引擎。

- 参数向量 → 层结构/仪器参数 的映射
- FOM 函数（chi2bars / log / diff / R1 / logR1）
- 差分进化（best_1_bin / rand_1_bin，可中断、逐代回调）

独立实现，未复制 GenX 源码。
"""

from __future__ import annotations

import copy
import math
import threading
import time
import uuid

import numpy as np

import core

EPS = 1e-12


# ----------------------------------------------------------------------------
# FOM
# ----------------------------------------------------------------------------
def compute_fom(name: str, y, sim, err=None) -> float:
    y = np.asarray(y, dtype=float)
    sim = np.asarray(sim, dtype=float)
    if y.shape != sim.shape:
        raise ValueError("数据与模拟长度不一致")

    if name == "chi2bars":
        if err is None:
            weight = np.ones_like(y)
        else:
            e = np.asarray(err, dtype=float)
            weight = np.where(np.abs(e) > 0, np.abs(e), 1.0)
        residual = (y - sim) / weight
        return float(np.mean(residual * residual))

    if name == "diff":
        return float(np.mean(np.abs(y - sim)))

    if name == "log":
        ly = np.log10(np.maximum(np.abs(y), EPS))
        ls = np.log10(np.maximum(np.abs(sim), EPS))
        return float(np.mean(np.abs(ly - ls)))

    if name == "R1":
        denom = float(np.sum(np.sqrt(np.abs(y))))
        num = float(np.sum(np.abs(np.sqrt(np.abs(y)) - np.sqrt(np.abs(sim)))))
        return num / max(denom, EPS)

    if name == "logR1":
        ly = np.log10(np.sqrt(np.maximum(np.abs(y), EPS)))
        ls = np.log10(np.sqrt(np.maximum(np.abs(sim), EPS)))
        return float(np.sum(np.abs(ly - ls)) / max(np.sum(np.abs(ly)), EPS))

    raise ValueError(f"未知 FOM：{name}")


FOM_NAMES = ["chi2bars", "log", "diff", "R1", "logR1"]


# ----------------------------------------------------------------------------
# 参数映射
# ----------------------------------------------------------------------------
def split_params(params: list[dict]):
    """拆成（拟合参数, 固定参数）。"""
    free = [p for p in params if p.get("fit")]
    fixed = [p for p in params if not p.get("fit")]
    return free, fixed


def apply_vector(stack: dict, instrument: dict, params: list[dict], vector=None) -> tuple[dict, dict]:
    """把参数值写回 stack/instrument 的深拷贝。"""
    st = copy.deepcopy(stack)
    inst = copy.deepcopy(instrument)
    values = {p["key"]: float(p["value"]) for p in params}
    if vector is not None:
        free, _ = split_params(params)
        for item, val in zip(free, vector):
            values[item["key"]] = float(val)

    for key, value in values.items():
        parts = key.split(".")
        if len(parts) < 2:
            continue
        head = parts[0]
        if head == "layer" and len(parts) >= 3:
            index = int(parts[1])
            field = parts[2]
            if 0 <= index < len(st.get("layers", [])):
                st["layers"][index][field] = value
        elif head == "seg" and len(parts) >= 5:
            seg_index = int(parts[1])
            layer_index = int(parts[3])
            field = parts[4]
            segments = st.get("segments") or []
            if 0 <= seg_index < len(segments):
                layers = segments[seg_index].get("layers") or []
                if 0 <= layer_index < len(layers):
                    layers[layer_index][field] = value
        elif head in ("ambient", "substrate"):
            st[head][parts[1]] = value
        elif head == "inst":
            field = parts[1]
            if field == "res_width":
                inst.setdefault("resolution", {})["width"] = value
            elif field == "res_points":
                inst.setdefault("resolution", {})["points"] = int(round(value))
            elif field == "res_spread":
                inst.setdefault("resolution", {})["spread"] = value
            elif field == "beam_width":
                inst.setdefault("footprint", {})["beam_width"] = value
            elif field == "sample_length":
                inst.setdefault("footprint", {})["sample_length"] = value
            else:
                inst[field] = value
    return st, inst


def bounds_of(params: list[dict]):
    free, _ = split_params(params)
    lo = []
    hi = []
    for p in free:
        low = float(p.get("min", 0.0))
        high = float(p.get("max", 0.0))
        if not (high > low):
            raise ValueError(f"参数 {p.get('label') or p['key']} 的上界必须大于下界")
        lo.append(low)
        hi.append(high)
    return np.array(lo, dtype=float), np.array(hi, dtype=float)


# ----------------------------------------------------------------------------
# 参数误差估计
# ----------------------------------------------------------------------------
def estimate_parameter_errors(residual_fn, best_vec, step_frac=1e-4):
    """数值 Jacobian -> 协方差 -> 参数标准误差。

    residual_fn(vector) 返回加权残差向量 (y - sim)/sigma。
    C = s^2 (J^T J)^-1，s^2 = sum(r^2)/(m-n)（约化卡方）。
    """
    n = len(best_vec)
    if n == 0:
        return np.array([])
    r0 = np.asarray(residual_fn(best_vec), dtype=float)
    m = r0.size
    if m <= n:
        return np.full(n, float("nan"))

    jac = np.zeros((m, n))
    for j in range(n):
        h = max(abs(best_vec[j]) * step_frac, 1e-6)
        xp = np.array(best_vec, dtype=float)
        xm = np.array(best_vec, dtype=float)
        xp[j] += h
        xm[j] -= h
        rp = np.asarray(residual_fn(xp), dtype=float)
        rm = np.asarray(residual_fn(xm), dtype=float)
        jac[:, j] = (rp - rm) / (2.0 * h)

    jtj = jac.T @ jac
    try:
        cov = np.linalg.inv(jtj)
    except np.linalg.LinAlgError:
        cov = np.linalg.pinv(jtj)

    dof = max(m - n, 1)
    s2 = float(np.sum(r0 * r0) / dof)
    cov = cov * s2
    return np.sqrt(np.abs(np.diag(cov)))


# ----------------------------------------------------------------------------
# 差分进化
# ----------------------------------------------------------------------------
def differential_evolution(evaluate, lo, hi, settings, on_generation, should_stop):
    """返回 (best_vector, best_fom, history)。"""
    dim = len(lo)
    if dim == 0:
        raise ValueError("没有勾选任何参与拟合的参数")

    strategy = settings.get("strategy") or "best_1_bin"
    f_weight = float(settings.get("f", 0.7))
    cr = float(settings.get("cr", 0.7))
    max_gen = int(settings.get("max_generations", 200))
    pop_size = int(settings.get("population", 0) or 0)
    if pop_size <= 0:
        pop_size = max(20, 3 * dim)
    spread_tol = float(settings.get("spread_tol", 0.0) or 0.0)
    seed = settings.get("seed")
    rng = np.random.default_rng(None if seed in (None, "", 0) else int(seed))

    span = hi - lo
    pop = lo + rng.random((pop_size, dim)) * span
    fitness = np.array([evaluate(ind) for ind in pop], dtype=float)
    best_idx = int(np.argmin(fitness))
    best_vec = pop[best_idx].copy()
    best_fom = float(fitness[best_idx])
    history = [best_fom]

    for gen in range(1, max_gen + 1):
        if should_stop():
            break
        for i in range(pop_size):
            candidates = [k for k in range(pop_size) if k != i]
            if strategy == "rand_1_bin":
                r1, r2, r3 = rng.choice(candidates, 3, replace=False)
                mutant = pop[r1] + f_weight * (pop[r2] - pop[r3])
            else:  # best_1_bin
                r1, r2 = rng.choice(candidates, 2, replace=False)
                mutant = best_vec + f_weight * (pop[r1] - pop[r2])

            mutant = np.clip(mutant, lo, hi)
            jrand = int(rng.integers(dim))
            mask = rng.random(dim) < cr
            mask[jrand] = True
            trial = np.where(mask, mutant, pop[i])

            trial_fom = evaluate(trial)
            if trial_fom < fitness[i]:
                pop[i] = trial
                fitness[i] = trial_fom
                if trial_fom < best_fom:
                    best_fom = float(trial_fom)
                    best_vec = trial.copy()

        history.append(best_fom)
        if on_generation is not None:
            on_generation(gen, best_fom, best_vec)

        if spread_tol > 0:
            relative = np.std(pop, axis=0) / np.maximum(span, EPS)
            if float(np.max(relative)) < spread_tol:
                break

    return best_vec, best_fom, history


# ----------------------------------------------------------------------------
# 作业
# ----------------------------------------------------------------------------
class FitJob:
    def __init__(self, payload: dict):
        self.id = uuid.uuid4().hex[:12]
        self.payload = payload
        self.state = "running"
        self.generation = 0
        self.max_generations = int((payload.get("algorithm") or {}).get("max_generations", 200))
        self.best_fom = None
        self.best_vector = None
        self.history: list[float] = []
        self.error = ""
        self.started = time.time()
        self.finished = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name=f"xrr-fit-{self.id}", daemon=True)

    # -- 控制 --------------------------------------------------------------
    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()

    def should_stop(self):
        return self._stop.is_set()

    # -- 主流程 ------------------------------------------------------------
    def _run(self):
        try:
            payload = self.payload
            data = payload.get("data") or {}
            x = np.asarray(data.get("x") or [], dtype=float)
            y = np.asarray(data.get("y") or [], dtype=float)
            err = data.get("error")
            if err:
                cleaned = [float(v) if v is not None and np.isfinite(v) else 0.0 for v in err]
                err = np.asarray(cleaned, dtype=float)
                if not np.any(err > 0):
                    err = None
            else:
                err = None
            if x.size < 3:
                raise ValueError("数据点太少，无法拟合")

            stack = payload.get("stack") or {}
            instrument = payload.get("instrument") or {}
            params = payload.get("params") or []
            settings = payload.get("algorithm") or {}
            fom_name = settings.get("fom") or "chi2bars"
            if fom_name not in FOM_NAMES:
                raise ValueError(f"未知 FOM：{fom_name}")

            lo, hi = bounds_of(params)
            free, _ = split_params(params)
            if len(free) != len(lo):
                raise ValueError("参数数量不一致")

            base_wavelength = float(instrument.get("wavelength") or 1.54)
            x_unit = data.get("x_unit") or "q"

            def simulate_with(vector):
                st, inst = apply_vector(stack, instrument, params, vector)
                return core.simulate({
                    "wavelength": float(inst.get("wavelength") or base_wavelength),
                    "x": x.tolist(),
                    "x_unit": x_unit,
                    "ambient": st.get("ambient") or {},
                    "substrate": st.get("substrate") or {},
                    "layers": st.get("layers") or [],
                    "segments": st.get("segments") or [],
                    "i0": float(inst.get("i0", 1.0)),
                    "ibkg": float(inst.get("ibkg", 0.0)),
                    "resolution": inst.get("resolution") or {},
                    "footprint": inst.get("footprint") or {},
                    "tthoff": float(inst.get("tthoff", 0.0) or 0.0),
                })

            def evaluate(vector):
                out = simulate_with(vector)
                sim = np.asarray(out["intensity"], dtype=float)
                return compute_fom(fom_name, y, sim, err)

            def on_generation(gen, fom, vec):
                with self._lock:
                    self.generation = gen
                    self.best_fom = fom
                    self.best_vector = vec.tolist()
                    self.history.append(fom)

            best_vec, best_fom, history = differential_evolution(
                evaluate, lo, hi, settings, on_generation, self.should_stop)

            # 用最优参数算一次终态曲线
            st, inst = apply_vector(stack, instrument, params, best_vec)
            final = core.simulate({
                "wavelength": float(inst.get("wavelength") or base_wavelength),
                "x": x.tolist(),
                "x_unit": x_unit,
                "ambient": st.get("ambient") or {},
                "substrate": st.get("substrate") or {},
                "layers": st.get("layers") or [],
                "segments": st.get("segments") or [],
                "i0": float(inst.get("i0", 1.0)),
                "ibkg": float(inst.get("ibkg", 0.0)),
                "resolution": inst.get("resolution") or {},
                "footprint": inst.get("footprint") or {},
                "tthoff": float(inst.get("tthoff", 0.0) or 0.0),
            })

            # 参数误差（数值 Jacobian -> 协方差）
            def residual_of(vector):
                out_e = simulate_with(vector)
                sim_e = np.asarray(out_e["intensity"], dtype=float)
                weight = err if err is not None else np.ones_like(y)
                weight = np.where(np.abs(weight) > 0, np.abs(weight), 1.0)
                return (y - sim_e) / weight

            try:
                err_values = estimate_parameter_errors(residual_of, best_vec)
                errors = {p["key"]: float(v) for p, v in zip(free, err_values)}
            except Exception:  # noqa: BLE001
                errors = {}

            with self._lock:
                self.best_fom = float(best_fom)
                self.best_vector = [float(v) for v in best_vec]
                self.history = [float(v) for v in history]
                self.final = final
                self.final_params = {
                    p["key"]: float(v) for p, v in zip(free, best_vec)
                }
                self.errors = errors
                self.state = "stopped" if self._stop.is_set() else "done"
                self.finished = time.time()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.state = "error"
                self.error = f"{type(exc).__name__}: {exc}"
                self.finished = time.time()

    # -- 状态 --------------------------------------------------------------
    def snapshot(self, include_final=True) -> dict:
        with self._lock:
            out = {
                "id": self.id,
                "state": self.state,
                "generation": self.generation,
                "max_generations": self.max_generations,
                "best_fom": self.best_fom,
                "history": list(self.history),
                "elapsed": (self.finished or time.time()) - self.started,
                "error": self.error,
                "best_vector": self.best_vector,
                "fit_keys": [p["key"] for p in (self.payload.get("params") or []) if p.get("fit")],
            }
            if include_final and self.state in ("done", "stopped") and hasattr(self, "final"):
                out["final_intensity"] = self.final["intensity"]
                out["final_params"] = getattr(self, "final_params", {})
                out["errors"] = getattr(self, "errors", {})
                out["best_vector"] = self.best_vector
            return out


JOBS: dict[str, FitJob] = {}
_JOBS_LOCK = threading.Lock()


def start_fit(payload: dict) -> dict:
    job = FitJob(payload)
    with _JOBS_LOCK:
        # 只保留最近 8 个作业
        if len(JOBS) > 8:
            for key in list(JOBS.keys())[: len(JOBS) - 8]:
                JOBS.pop(key, None)
        JOBS[job.id] = job
    job.start()
    return {"job_id": job.id, "max_generations": job.max_generations}


def fit_status(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise KeyError(f"没有作业 {job_id}")
    return job.snapshot()


def fit_stop(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        raise KeyError(f"没有作业 {job_id}")
    job.stop()
    return {"job_id": job_id, "stopping": True}
