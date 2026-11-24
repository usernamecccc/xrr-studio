"""XRR 计算内核。

独立实现：材料散射因子与 SLD、Parratt 递推、Nevot–Croce 粗糙度、
分辨率卷积、footprint 修正、I0/本底。
数学参考 GenX 3.8.14 的公开算法（未复制其源码）。

层序约定（对外）：ambient -> layer_1 ... layer_N -> substrate
内部递推使用 substrate -> ... -> ambient（与 Parratt 递推的自然方向一致）。
"""

from __future__ import annotations

import cmath
import functools
import math
import re
import sys
from pathlib import Path

# 宿主自带 Python 未预装 numpy，依赖随插件放在 vendor/
_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

import numpy as np

# numba 并行版 Parratt：单线程与 numpy 相当，但按 q 点 prange 多核并行后可快 4~6 倍
# （151 层 × 1001 点：16 ms → 3 ms）。numba 不可用时自动回退到 numpy 向量化实现。
_HAS_NUMBA = False
_njit = None
_prange = None
try:  # pragma: no cover - 依赖可选组件
    from numba import njit as _njit_mod, prange as _prange_mod

    _njit = _njit_mod
    _prange = _prange_mod
    _HAS_NUMBA = True
except Exception:  # noqa: BLE001
    _HAS_NUMBA = False

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "materials"

R_E = 2.8179403262e-5          # 经典电子半径 [Å]
HC_EV_A = 12398.419843320026   # E[eV] x lambda[Å]
N_AVOGADRO = 6.02214076e23     # 1/mol

# 空介质（真空/空气）：SLD 记为 0
_EMPTY_MEDIA = {"", "air", "vacuum", "真空", "真空层"}


# ----------------------------------------------------------------------------
# 元素数据
# ----------------------------------------------------------------------------
class Elements:
    """元素级数据：原子量、Cromer–Mann f0 系数、NIST f1/f2 表。"""

    def __init__(self, data_dir: Path = DATA_DIR):
        self.dir = Path(data_dir)
        self.weights: dict[str, float] = {}
        self.f0_coeffs: dict[str, tuple[float, ...]] = {}
        self._nff: dict[str, np.ndarray] = {}
        self._f1f2_cache: dict[tuple[str, float], tuple[float, float]] = {}
        self._load_weights()
        self._load_f0()

    # -- 原子量（DABAX 格式：每元素取丰度最高的同位素对应的标准原子量）----
    def _load_weights(self) -> None:
        path = self.dir / "AtomicWeights.dat"
        if not path.exists():
            return
        best: dict[str, tuple[float, float]] = {}
        sym = None
        expect = 0
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if line.startswith("#S"):
                parts = line[2:].split()
                sym = None
                if len(parts) >= 2:
                    sym = re.sub(r"^\d+", "", parts[1])
                expect = 0
            elif line.startswith("#N"):
                try:
                    expect = int(line[2:].strip().split()[0])
                except (ValueError, IndexError):
                    expect = 0
            elif line.startswith("#") or not line:
                continue
            elif sym and expect > 0:
                vals = line.split()
                if len(vals) >= 3:
                    try:
                        abundance = float(vals[1])
                        averaged = float(vals[2])
                    except ValueError:
                        continue
                    prev = best.get(sym)
                    if prev is None or abundance > prev[0]:
                        best[sym] = (abundance, averaged)
                expect -= 1
        self.weights = {k: v[1] for k, v in best.items()}

    # -- Cromer–Mann f0 系数（中性原子条目）-------------------------------
    def _load_f0(self) -> None:
        path = self.dir / "f0_CromerMann.dat"
        if not path.exists():
            return
        neutral = re.compile(r"^#S\s+\d+\s+([A-Z][a-z]?)\s*$")
        sym = None
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.rstrip()
            if line.startswith("#S"):
                m = neutral.match(line)
                sym = m.group(1) if m else None
            elif sym and line and not line.startswith("#"):
                vals = line.split()
                if len(vals) == 9:
                    try:
                        coeffs = tuple(float(v) for v in vals)
                    except ValueError:
                        sym = None
                        continue
                    self.f0_coeffs.setdefault(sym, coeffs)
                    sym = None

    # -- f1/f2 表（NIST FFAST：E(eV) f1 f2）-----------------------------
    def nff(self, symbol: str) -> np.ndarray:
        key = symbol.strip()
        if key in self._nff:
            return self._nff[key]
        path = self.dir / "f1f2_nist" / f"{key.lower()}.nff"
        if not path.exists():
            raise KeyError(f"缺少元素 {key} 的 f1/f2 数据")
        table = np.loadtxt(path, skiprows=1)
        if table.ndim != 2 or table.shape[1] < 3:
            raise ValueError(f"{key}.nff 数据格式异常")
        self._nff[key] = table
        return table

    def f1f2(self, symbol: str, energy_ev: float) -> tuple[float, float]:
        # 拟合时同一波长会被反复查询同一元素，缓存命中率接近 100%
        key = (symbol, energy_ev)
        cached = self._f1f2_cache.get(key)
        if cached is not None:
            return cached
        table = self.nff(symbol)
        f1 = float(np.interp(energy_ev, table[:, 0], table[:, 1]))
        f2 = float(np.interp(energy_ev, table[:, 0], table[:, 2]))
        self._f1f2_cache[key] = (f1, f2)
        return f1, f2

    def f0(self, symbol: str, sin_theta_over_lambda: float) -> float:
        coeffs = self.f0_coeffs.get(symbol)
        if coeffs is None:
            return 0.0
        a1, a2, a3, a4, c, b1, b2, b3, b4 = coeffs
        k2 = float(sin_theta_over_lambda) ** 2
        return (
            c
            + a1 * math.exp(-b1 * k2)
            + a2 * math.exp(-b2 * k2)
            + a3 * math.exp(-b3 * k2)
            + a4 * math.exp(-b4 * k2)
        )

    def known(self, symbol: str) -> bool:
        try:
            self.nff(symbol)
            return True
        except (KeyError, OSError, ValueError):
            return False


ELEMENTS = Elements()


# 常用材料的理论块体密度（g/cm³），用于计算「拟合密度占 bulk 的百分比」
BULK_DENSITY: dict[str, float] = {
    "Air": 0.0012, "Vacuum": 0.0,
    "Si": 2.329, "SiO2": 2.20, "Si3N4": 3.17, "SiC": 3.21,
    "Mo": 10.28, "MoSi2": 6.24, "Mo5Si3": 7.76,
    "Y": 4.469, "Y2O3": 5.01,
    "Ti": 4.506, "TiO2": 4.23, "Cr": 7.19, "Fe": 7.874, "Ni": 8.908,
    "Cu": 8.96, "W": 19.25, "Au": 19.32, "Ag": 10.49, "Pt": 21.45,
    "Ta": 16.65, "Ta2O5": 8.20, "Nb2O5": 4.60, "Ru": 12.37,
    "Al": 2.70, "Al2O3": 3.95, "AlN": 3.26, "MgO": 3.58, "ZnO": 5.606,
    "HfO2": 9.68, "ZrO2": 5.68, "Ge": 5.323, "GaAs": 5.317, "InP": 4.81,
    "B": 2.34, "B4C": 2.52, "C": 3.51, "TiN": 5.43, "VN": 6.13,
}


def bulk_density(formula: str) -> float | None:
    """查材料的理论块体密度（g/cm³）；未知返回 None。"""
    if not formula:
        return None
    key = formula.replace(" ", "")
    if key in BULK_DENSITY:
        return BULK_DENSITY[key]
    for name, value in BULK_DENSITY.items():
        if name.lower() == key.lower():
            return value
    return None


# ----------------------------------------------------------------------------
# 化学式
# ----------------------------------------------------------------------------
_TOKEN = re.compile(r"([A-Z][a-z]?)(\d*\.?\d*)")


@functools.lru_cache(maxsize=1024)
def parse_formula(formula: str) -> dict[str, float]:
    """解析化学式，支持嵌套括号与非整数计量：MoSi2 / Si3N4 / Mo0.8Y0.2 / Ag2(NO2)3。"""
    text = (formula or "").replace(" ", "")
    if not text:
        raise ValueError("化学式为空")

    def read_number(s: str, i: int) -> tuple[float, int]:
        m = re.match(r"\d*\.?\d+", s[i:])
        if not m:
            return 1.0, i
        return float(m.group(0)), i + m.end()

    def parse_block(s: str, i: int) -> tuple[dict[str, float], int]:
        counts: dict[str, float] = {}
        while i < len(s):
            ch = s[i]
            if ch == "(":
                inner, i = parse_block(s, i + 1)
                factor, i = read_number(s, i)
                for key, val in inner.items():
                    counts[key] = counts.get(key, 0.0) + val * factor
            elif ch == ")":
                return counts, i + 1
            elif ch.isupper():
                m = _TOKEN.match(s, i)
                if not m:
                    raise ValueError(f"化学式无法解析：{formula}")
                symbol = m.group(1)
                number = float(m.group(2)) if m.group(2) else 1.0
                counts[symbol] = counts.get(symbol, 0.0) + number
                i = m.end()
            else:
                raise ValueError(f"化学式无法解析：{formula}（位置 {i}）")
        return counts, i

    counts, _ = parse_block(text, 0)
    if not counts:
        raise ValueError(f"化学式无法解析：{formula}")
    return counts


@functools.lru_cache(maxsize=1024)
def formula_mass(formula: str) -> float:
    """化学式摩尔质量 [g/mol]。"""
    counts = parse_formula(formula)
    total = 0.0
    for symbol, number in counts.items():
        weight = ELEMENTS.weights.get(symbol)
        if weight is None:
            raise KeyError(f"缺少元素 {symbol} 的原子量")
        total += weight * number
    return total


# ----------------------------------------------------------------------------
# SLD
# ----------------------------------------------------------------------------
def material_sld(formula: str, density: float, wavelength: float) -> complex:
    """X 射线 SLD [Å^-2]：rho_e * lambda^2 / (2*pi)，rho_e 为电子密度。"""
    if (formula or "").strip().lower() in _EMPTY_MEDIA:
        return 0j
    if wavelength <= 0:
        raise ValueError("波长必须为正")
    counts = parse_formula(formula)
    molar_mass = formula_mass(formula)
    if molar_mass <= 0 or density <= 0:
        raise ValueError("密度与摩尔质量必须为正")
    number_density = density * N_AVOGADRO / molar_mass * 1e-24   # 化学式单位 / Å^3
    energy = HC_EV_A / wavelength
    scattering = 0j
    for symbol, number in counts.items():
        f1, f2 = ELEMENTS.f1f2(symbol, energy)
        scattering += number * (f1 - 1j * f2)
    return number_density * scattering * wavelength ** 2 / (2.0 * math.pi)


def refractive_index(sld: complex) -> complex:
    """n = 1 - r_e * SLD（X 射线近似）。"""
    return 1.0 - R_E * sld


# ----------------------------------------------------------------------------
# 层堆栈
# ----------------------------------------------------------------------------
class Layer:
    __slots__ = ("formula", "density", "thickness", "roughness", "sld")

    def __init__(self, formula: str, density: float, thickness: float = 0.0,
                 roughness: float = 0.0, sld: complex | None = None):
        self.formula = formula
        self.density = float(density)
        self.thickness = float(thickness)
        self.roughness = float(roughness)
        self.sld = sld

    def resolve_sld(self, wavelength: float) -> complex:
        if self.sld is not None:
            return self.sld
        if not self.formula:
            raise ValueError("层缺少材料")
        return material_sld(self.formula, self.density, wavelength)


MAX_TOTAL_LAYERS = 4000


def expand_layers(payload: dict) -> list[dict]:
    """展开层结构。

    优先使用 segments（每个分组可重复 N 次）：
        segments: [{"repetitions": 50, "layers": [{formula, density, thickness, roughness}, ...]}, ...]
    否则回退到扁平的 layers 列表。
    """
    segments = payload.get("segments")
    if not segments:
        return payload.get("layers") or []

    out: list[dict] = []
    for seg in segments:
        layers = seg.get("layers") or []
        if not layers:
            continue
        reps = int(seg.get("repetitions") or 1)
        reps = max(1, min(reps, 1000))
        for _ in range(reps):
            out.extend(layers)
            if len(out) > MAX_TOTAL_LAYERS:
                raise ValueError(
                    f"层数超过上限 {MAX_TOTAL_LAYERS}，请降低重复次数或层数")
    if not out:
        raise ValueError("层结构为空")
    return out


def build_stack(payload: dict, wavelength: float):
    """把请求参数整理成 (sld[], thickness[], roughness[])。

    sld 顺序：ambient -> layers -> substrate
    roughness 顺序：ambient/layer1 ... layerN/substrate，长度 = N + 1
    """
    ambient = payload.get("ambient") or {}
    substrate = payload.get("substrate") or {}
    raw_layers = expand_layers(payload)

    amb_layer = Layer(ambient.get("formula", ""), ambient.get("density", 0.0),
                      sld=ambient.get("sld"))
    sub_layer = Layer(substrate.get("formula", ""), substrate.get("density", 0.0),
                      roughness=substrate.get("roughness", 0.0) or 0.0,
                      sld=substrate.get("sld"))

    slds = [amb_layer.resolve_sld(wavelength)]
    thickness: list[float] = []
    roughness: list[float] = []

    for item in raw_layers:
        layer = Layer(item.get("formula", ""), item.get("density", 0.0),
                      item.get("thickness", 0.0), item.get("roughness", 0.0),
                      sld=item.get("sld"))
        slds.append(layer.resolve_sld(wavelength))
        thickness.append(layer.thickness)
        roughness.append(layer.roughness)

    slds.append(sub_layer.resolve_sld(wavelength))

    # 界面粗糙度语义（与 GenX 一致）：第 i 个界面取第 i 层的 roughness，
    # 即「该层与上方层的界面」；最后一个界面（最下层/衬底）沿用最下层的值。
    sub_roughness = float(substrate.get("roughness", 0.0) or 0.0)
    if not roughness:
        roughness = [sub_roughness]
    else:
        roughness = list(roughness)
        roughness.append(sub_roughness)
        roughness = roughness[: len(thickness) + 1]
    return slds, thickness, roughness


# ----------------------------------------------------------------------------
# Parratt 递推
# ----------------------------------------------------------------------------
if _HAS_NUMBA:

    @_njit(cache=True, parallel=True, nogil=True)
    def _parratt_jit(q, n, d, sigma, q0):
        """按 q 点并行：每个角度独立走一遍逐层递推（与 GenX ReflQNB 同思路）。"""
        n_q = q.size
        n_layer = n.size
        n_amb = n[n_layer - 1]
        out = np.empty(n_q, dtype=np.float64)
        for i in _prange(n_q):
            base = n_amb * n_amb * q[i] * q[i]
            q_prev = cmath.sqrt((n[0] * n[0] - n_amb * n_amb) * q0 * q0 + base)
            r = 0j
            for j in range(1, n_layer):
                q_cur = cmath.sqrt((n[j] * n[j] - n_amb * n_amb) * q0 * q0 + base)
                rp = (q_cur - q_prev) / (q_cur + q_prev) * cmath.exp(
                    -q_cur * q_prev / 2.0 * sigma[j - 1] * sigma[j - 1])
                if j == 1:
                    r = rp
                else:
                    phase = cmath.exp(1j * d[j - 1] * q_prev)
                    part = r * phase
                    r = (rp + part) / (1.0 + part * rp)
                q_prev = q_cur
            out[i] = r.real * r.real + r.imag * r.imag
        return out


def parratt(q: np.ndarray, slds: list[complex], thickness: list[float],
            roughness: list[float], wavelength: float) -> np.ndarray:
    """返回反射率 |r|^2。q 单位 Å^-1。"""
    q = np.asarray(q, dtype=float)
    n_layers = len(thickness)
    if n_layers != len(slds) - 2:
        raise ValueError("层数与 SLD 数量不匹配")

    n = np.array([refractive_index(s) for s in reversed(slds)], dtype=complex)  # sub -> amb
    d = np.array([0.0] + list(reversed(thickness)) + [0.0], dtype=float)
    sigma = np.array(list(reversed(roughness)), dtype=float)  # sub/layerN ... layer1/amb

    q0 = 4.0 * math.pi / wavelength

    # numba 并行有约 7 ms 的线程调度开销：只有规模够大（层数 × 点数 ≥ 6 万）才划算，
    # 更小的结构直接用 numpy 向量化更快（实测 42 层 × 1001 点时 numpy 快约 25%）
    if _HAS_NUMBA and q.size >= 64 and n_layers * q.size >= 60000:
        try:
            return _parratt_jit(q, n, d, sigma, q0)
        except Exception:  # noqa: BLE001
            # 编译失败（缺少编译器/线程库）时永久回退，避免每次评估都抛异常
            globals()["_HAS_NUMBA"] = False

    n_amb = n[-1]
    qj = np.sqrt((n[:, None] ** 2 - n_amb ** 2) * q0 ** 2 + n_amb ** 2 * q[None, :] ** 2)

    fresnel = (qj[1:] - qj[:-1]) / (qj[1:] + qj[:-1])
    fresnel = fresnel * np.exp(-qj[1:] * qj[:-1] / 2.0 * sigma[:, None] ** 2)
    phase = np.exp(1j * d[1:-1, None] * qj[1:-1])

    r = fresnel[0]
    for j in range(1, fresnel.shape[0]):
        r = (fresnel[j] + r * phase[j - 1]) / (1.0 + r * fresnel[j] * phase[j - 1])
    return np.abs(r) ** 2


# ----------------------------------------------------------------------------
# 仪器修正
# ----------------------------------------------------------------------------
def resolution_convolve(q: np.ndarray, reflectivity: np.ndarray, restype: str,
                        width: float, points: int, spread: float) -> np.ndarray:
    """高斯分辨率卷积。restype: none / fast（固定宽度）/ relative（宽度随 q）。"""
    if restype in (None, "", "none"):
        return reflectivity
    width_array = np.asarray(width, dtype=float)
    if width_array.ndim == 0:
        if float(width_array) <= 0:
            return reflectivity
    elif not bool(np.any(width_array > 0)):
        return reflectivity
    points = max(3, int(points))
    spread = max(1.0, float(spread))
    offsets = np.linspace(-spread, spread, points)
    weights = np.exp(-0.5 * offsets ** 2)
    weights /= weights.sum()

    if restype == "relative":
        sigma = np.maximum(np.abs(q) * np.asarray(width, dtype=float), 1e-9)
    else:
        sigma = np.asarray(width, dtype=float)
        if sigma.ndim == 0:
            sigma = np.full_like(q, float(sigma), dtype=float)

    result = np.zeros_like(reflectivity)
    for off, weight in zip(offsets, weights):
        shifted_q = q + off * sigma
        result += weight * np.interp(shifted_q, q, reflectivity, left=reflectivity[0],
                                     right=reflectivity[-1])
    return result


def resolution_convolve_calc(q, slds, thickness, roughness, wavelength,
                             sigma, points, spread):
    """按 GenX 的 fast conv 方式：每个采样点重新计算 Parratt 后加权平均。"""
    q = np.asarray(q, dtype=float)
    sigma_array = np.asarray(sigma, dtype=float)
    if sigma_array.ndim == 0:
        sigma_array = np.full_like(q, float(sigma_array))
    if not bool(np.any(sigma_array > 0)):
        return parratt(q, slds, thickness, roughness, wavelength)
    points = max(3, int(points))
    spread = max(1.0, float(spread))
    offsets = np.linspace(-spread, spread, points)
    weights = np.exp(-0.5 * offsets ** 2)
    weights = weights / weights.sum()
    # 一次性把所有偏移的 q 拼成一维数组，单次 Parratt 调用（比循环快约 2 倍）
    q2d = q[None, :] + offsets[:, None] * sigma_array[None, :]
    flat = parratt(q2d.ravel(), slds, thickness, roughness, wavelength)
    r2d = flat.reshape(points, q.size)
    return (weights[:, None] * r2d).sum(axis=0)


def _uniform_step(x: np.ndarray) -> float | None:
    """等间隔则返回步长，否则返回 None。"""
    if x.size < 3:
        return None
    diff = np.diff(x)
    if not bool(np.all(diff > 0)):
        return None
    step = float(np.mean(diff))
    if step <= 0:
        return None
    if float(np.max(np.abs(diff - step))) > 1e-6 * max(abs(step), 1.0):
        return None
    return step


def resolution_convolve_x(x, reflectivity, restype: str, width, points: int,
                          spread: float) -> np.ndarray:
    """在横坐标域做分辨率卷积，与 GenX 的 ConvoluteFast / ConvoluteFastVar 对齐。

    GenX 的 fast conv 直接在实验横坐标（2θ 或 q）上卷积反射率，核宽以该坐标
    单位给出；等间隔数据用 arange 建核 + numpy.convolve，与 GenX 位级一致。
    非等间隔（用户实测数据）退回偏移采样插值，核宽语义不变。
    """
    x = np.asarray(x, dtype=float)
    r = np.asarray(reflectivity, dtype=float)
    if r.shape != x.shape:
        return r
    width_value = float(np.max(np.asarray(width, dtype=float))) if np.ndim(width) else float(width)
    if width_value <= 0:
        return r

    # 横坐标递减时先翻转为递增，卷积完再翻回来
    if x.size > 1 and x[0] > x[-1]:
        flipped = resolution_convolve_x(x[::-1], r[::-1], restype, width, points, spread)
        return flipped[::-1]

    points = max(3, int(points))
    spread = max(1.0, float(spread))

    if restype == "relative":
        dq = np.maximum(np.abs(x) * float(width), 1e-12)
    else:
        dq = np.full_like(x, float(width))

    step = _uniform_step(x)
    if step is not None and float(np.ptp(dq)) <= 1e-9 * max(1.0, float(np.max(dq))):
        # 等间隔 + 常数核宽：照 GenX ConvoluteFast 实现
        sigma = float(dq[0])
        resvector = np.arange(-spread * sigma, spread * sigma + step, step)
        if resvector.size < 2:
            return r
        weight = np.exp(-0.5 * (resvector / sigma) ** 2)
        weight /= weight.sum()
        pad = resvector.size
        padded = np.concatenate((np.full(pad, r[0]), r, np.full(pad, r[-1])))
        return np.convolve(padded, weight, mode="same")[pad:-pad]

    # 非等间隔或核宽随坐标变化：按偏移采样插值
    offsets = np.linspace(-spread, spread, points)
    weights = np.exp(-0.5 * offsets ** 2)
    weights /= weights.sum()
    result = np.zeros_like(r)
    for offset, weight in zip(offsets, weights):
        shifted = np.interp(x + offset * dq, x, r, left=r[0], right=r[-1])
        result += weight * shifted
    return result


def footprint_factor(q: np.ndarray, footype: str, beam_width: float,
                     sample_length: float, wavelength: float) -> np.ndarray:
    """照射面积修正（高斯束 / 方形束），与 GenX 的 GaussIntensity / SquareIntensity 一致。"""
    if footype in (None, "", "none") or beam_width <= 0 or sample_length <= 0:
        return np.ones_like(q, dtype=float)
    theta = np.arcsin(np.clip(q * wavelength / (4.0 * math.pi), 0.0, 0.999))
    sinalpha = np.sin(theta)
    if footype == "square":
        factor = sample_length / beam_width * sinalpha
        return np.where(factor <= 1.0, factor, 1.0)
    # 高斯束：GenX 调用 GaussIntensity(theta, samplelen/2, samplelen/2, beamw)
    erf = np.vectorize(math.erf)
    factor = erf(np.clip(sample_length * sinalpha / (2.0 * math.sqrt(2.0) * beam_width),
                         0.0, 6.0))
    return np.clip(factor, 0.0, 1.0)


# ----------------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------------
def simulate(payload: dict) -> dict:
    """执行一次仿真。

    payload:
      wavelength: float [Å]
      q: list[float] 或 x + x_unit ('q' | '2theta')
      ambient / substrate: {formula, density} 或 {sld: [re, im]}
      layers: [{formula, density, thickness, roughness} | {sld: [re, im], thickness, roughness}]
      i0, ibkg: float
      resolution: {type: none|fast|relative, width, points, spread}
      footprint: {type: none|gauss|square, beam_width, sample_length}
    """
    wavelength = float(payload.get("wavelength") or 1.54)
    if wavelength <= 0:
        raise ValueError("波长必须为正")

    # 兼容 numpy 数组（拟合内循环直接传数组，省掉 list 往返转换）
    x = payload.get("q")
    if x is None:
        x = payload.get("x")
    if x is None or len(x) == 0:
        raise ValueError("缺少 q 或 x 数据")
    x = np.asarray(x, dtype=float)
    unit = (payload.get("x_unit") or "q").lower()
    tthoff = float(payload.get("tthoff") or 0.0)
    if unit in ("q", "qinv", ""):
        q = x
    elif unit in ("theta", "θ", "th", "angle"):
        # 入射角（度）：q = 4π·sin(θ)/λ（角度偏移按 2θ 定义，折半）
        q = 4.0 * math.pi * np.sin(np.radians(x + tthoff / 2.0)) / wavelength
    else:
        # 散射角 2θ（度）
        q = 4.0 * math.pi * np.sin(np.radians((x + tthoff) / 2.0)) / wavelength

    slds, thickness, roughness = build_stack(payload, wavelength)
    reflectivity = parratt(q, slds, thickness, roughness, wavelength)

    # 顺序与 GenX 一致（genx/models/spec_nx.py:resolutioncorr）：
    # 先乘照射面积修正，再在实验横坐标上做分辨率卷积
    foot = payload.get("footprint") or {}
    reflectivity = reflectivity * footprint_factor(
        q,
        footype=(foot.get("type") or "none").lower(),
        beam_width=float(foot.get("beam_width") or 0.0),
        sample_length=float(foot.get("sample_length") or 0.0),
        wavelength=wavelength,
    )

    res = payload.get("resolution") or {}
    res_type = (res.get("type") or "none").lower()
    res_width = float(res.get("width") or 0.0)
    res_points = int(res.get("points") or 5)
    res_spread = float(res.get("spread") or 2.0)
    if res_type in ("fast", "relative") and res_width > 0:
        reflectivity = resolution_convolve_x(
            x, reflectivity, res_type, res_width, res_points, res_spread)

    i0 = float(payload.get("i0", 1.0))
    ibkg = float(payload.get("ibkg", 0.0))
    intensity = i0 * reflectivity + ibkg

    return {
        "q": q.tolist(),
        "reflectivity": reflectivity.tolist(),
        "intensity": intensity.tolist(),
        "slds": [[s.real, s.imag] for s in slds],
        "wavelength": wavelength,
    }


def sld_profile(payload: dict) -> dict:
    """深度方向的 SLD 剖面（用于 SLD 页）。"""
    wavelength = float(payload.get("wavelength") or 1.54)
    slds, thickness, _ = build_stack(payload, wavelength)
    depth = [0.0]
    values = [slds[0]]
    position = 0.0
    for idx, thick in enumerate(thickness):
        position += thick
        depth.append(position)
        values.append(slds[idx + 1])
    depth.append(position)
    values.append(slds[-1])
    return {
        "depth": depth,
        "sld": [[v.real, v.imag] for v in values],
    }


def materials() -> dict:
    """可用元素与常用材料密度。"""
    return {
        "elements": sorted(ELEMENTS.weights.keys()),
        "bulk": BULK_DENSITY,
        "presets": [
            {"formula": "Si", "density": 2.329},
            {"formula": "SiO2", "density": 2.20},
            {"formula": "Mo", "density": 10.28},
            {"formula": "MoSi2", "density": 6.24},
            {"formula": "Y", "density": 4.469},
            {"formula": "Si3N4", "density": 3.17},
            {"formula": "Al2O3", "density": 3.95},
            {"formula": "Ti", "density": 4.506},
            {"formula": "Cr", "density": 7.19},
            {"formula": "Fe", "density": 7.874},
            {"formula": "Ni", "density": 8.908},
            {"formula": "Cu", "density": 8.96},
            {"formula": "W", "density": 19.25},
            {"formula": "Au", "density": 19.30},
            {"formula": "Air", "density": 0.0012},
        ],
    }
