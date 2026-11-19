"""GenX 项目文件（.hgx）导入。

只做静态解析（ast），不执行 GenX 脚本。解析内容：
  数据（x / y / error）、模型脚本里的层与栈、参数表、仪器设置、FOM。
转换为本插件可直接使用的 payload。
"""

from __future__ import annotations

import ast
import os
import tempfile
from pathlib import Path

import h5py
import numpy as np

import core

NA_FACTOR = 0.6022

FIELD_MAP = {"setD": "thickness", "setDens": "density", "setSigma": "roughness"}
INST_MAP = {
    "setI0": "i0", "setIbkg": "ibkg", "setRes": "res_width",
    "setBeamw": "beam_width", "setSamplelen": "sample_length", "setTthoff": "tthoff",
}
INST_LABEL = {
    "i0": "I₀", "ibkg": "本底 I_bkg", "res_width": "分辨率宽度 σ",
    "beam_width": "束宽 (mm)", "sample_length": "样品长 (mm)", "tthoff": "角度偏移 (°)",
}
FIELD_LABEL = {"thickness": "厚度", "density": "密度", "roughness": "粗糙度"}

FOOT_MAP = {"no corr": "none", "gauss beam": "gauss", "square beam": "square"}


def _safe_eval(node):
    """仅允许字面量与四则运算。"""
    expr = ast.Expression(body=node)
    code = compile(expr, "<hgx>", "eval")
    return eval(code, {"__builtins__": {}}, {})


def _counts_from_f(node) -> dict:
    """从 f=fp.Ni*93+fp.V*7 这类表达式提取 {元素: 计量}。"""
    counts: dict[str, float] = {}
    if node is None:
        return counts
    for sub in ast.walk(node):
        if (isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Mult)
                and isinstance(sub.left, ast.Attribute)
                and isinstance(sub.left.value, ast.Name) and sub.left.value.id == "fp"
                and isinstance(sub.right, ast.Constant)
                and isinstance(sub.right.value, (int, float))):
            counts[sub.left.attr] = float(sub.right.value)
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                and sub.value.id == "fp"):
            counts.setdefault(sub.attr, 1.0)
    return counts


def _formula_of(counts: dict) -> str:
    """把 {Ni: 93, V: 7} 写成化学式字符串 Ni93V7。"""
    parts = []
    for element, number in counts.items():
        if abs(number - round(number)) < 1e-9:
            value = int(round(number))
            parts.append(element if value == 1 else f"{element}{value}")
        else:
            parts.append(f"{element}{number:g}")
    return "".join(parts)


def _molar_mass(counts: dict) -> float:
    total = 0.0
    for element, number in counts.items():
        weight = core.ELEMENTS.weights.get(element)
        if weight:
            total += weight * number
    return total


def _kwarg(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _names(node):
    if isinstance(node, (ast.List, ast.Tuple)):
        return [e.id for e in node.elts if isinstance(e, ast.Name)]
    return []


def parse_script(script: str):
    """返回 (layers, stacks, sample, instrument)。"""
    tree = ast.parse(script)
    layers: dict[str, dict] = {}
    stacks: dict[str, dict] = {}
    sample = None
    instrument: dict = {}

    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        name = target.id
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        attr = func.attr if isinstance(func, ast.Attribute) else None

        if attr == "Layer":
            layers[name] = {
                "d": _safe_eval(_kwarg(call, "d")) if _kwarg(call, "d") is not None else 0.0,
                "dens": _safe_eval(_kwarg(call, "dens")) if _kwarg(call, "dens") is not None else 0.0,
                "sigma": _safe_eval(_kwarg(call, "sigma")) if _kwarg(call, "sigma") is not None else 0.0,
                "counts": _counts_from_f(_kwarg(call, "f")),
            }
        elif attr == "Stack":
            stacks[name] = {
                "layers": _names(_kwarg(call, "Layers")),
                "repetitions": int(_safe_eval(_kwarg(call, "Repetitions")))
                if _kwarg(call, "Repetitions") is not None else 1,
            }
        elif attr == "Sample":
            amb = _kwarg(call, "Ambient")
            sub = _kwarg(call, "Substrate")
            sample = {
                "stacks": _names(_kwarg(call, "Stacks")),
                "ambient": amb.id if isinstance(amb, ast.Name) else None,
                "substrate": sub.id if isinstance(sub, ast.Name) else None,
            }
        elif attr == "Instrument":
            for key in ("wavelength", "I0", "Ibkg", "coords", "restype", "res",
                        "respoints", "resintrange", "footype", "beamw", "samplelen"):
                value_node = _kwarg(call, key)
                if value_node is None:
                    continue
                try:
                    instrument[key] = _safe_eval(value_node)
                except Exception:  # noqa: BLE001
                    continue

    return layers, stacks, sample, instrument


def _density_gcm3(dens_per_a3: float, counts: dict) -> float:
    """GenX 的 dens 是「每 Å³ 的化学式单位数」，乘以化学式摩尔质量得 g/cm³。"""
    if not counts:
        return 0.0012
    molar = _molar_mass(counts)
    if molar <= 0:
        return 0.0
    return float(dens_per_a3) * molar / NA_FACTOR


def _read_text(group, key, default=""):
    if key not in group:
        return default
    value = group[key][()]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, np.bytes_):
        return value.tobytes().decode("utf-8", errors="replace")
    return str(value)


def load_bytes(raw: bytes, name: str = "") -> dict:
    """从内存中的 .hgx 内容解析（浏览器只能提供内容，拿不到完整路径）。"""
    fd, temp_path = tempfile.mkstemp(suffix=".hgx")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        result = load(temp_path)
        if name:
            result["meta"]["source"] = name
        result["meta"]["from_bytes"] = True
        return result
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def load(path: str) -> dict:
    """读取 .hgx 并转换为插件 payload。"""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"找不到文件：{path}")
    if file_path.suffix.lower() != ".hgx":
        raise ValueError("只支持 .hgx 项目文件")

    with h5py.File(file_path, "r") as fh:
        if "current" not in fh:
            raise ValueError("不是有效的 GenX 项目文件（缺少 current 组）")
        current = fh["current"]

        ds = current["data/datasets/0"]
        x = np.asarray(ds["x"][()], dtype=float)
        y = np.asarray(ds["y"][()], dtype=float)
        error = np.asarray(ds["error"][()], dtype=float) if "error" in ds else None
        x_raw = np.asarray(ds["x_raw"][()], dtype=float) if "x_raw" in ds else None
        x_command = _read_text(ds, "x_command", "x")

        script = _read_text(current, "script", "")
        fom = _read_text(current, "fomfunction", "chi2bars")

        params_group = current["parameters"]
        raw_keys = params_group["data col 0"][()]
        param_keys = []
        for item in raw_keys:
            if isinstance(item, bytes):
                param_keys.append(item.decode("utf-8", errors="replace"))
            elif isinstance(item, np.bytes_):
                param_keys.append(item.tobytes().decode("utf-8", errors="replace"))
            else:
                param_keys.append(str(item))
        param_values = np.asarray(params_group["data col 1"][()], dtype=float)
        param_fit = np.asarray(params_group["data col 2"][()], dtype=bool)
        param_min = np.asarray(params_group["data col 3"][()], dtype=float)
        param_max = np.asarray(params_group["data col 4"][()], dtype=float)

    layers, stacks, sample, instrument = parse_script(script)
    if not sample or not sample.get("stacks"):
        raise ValueError("脚本里没有可识别的 Sample/Stack 定义")

    # 段：GenX 的 Stacks 顺序是「先底部」，插件是「先 ambient」，故反转
    segments = []
    name_pos: dict[str, tuple[int, int]] = {}
    for seg_index, stack_name in enumerate(reversed(sample["stacks"])):
        stack = stacks.get(stack_name)
        if not stack:
            continue
        layer_defs = []
        for layer_index, layer_name in enumerate(reversed(stack["layers"])):
            definition = layers.get(layer_name, {})
            counts = definition.get("counts") or {}
            layer_defs.append({
                "formula": _formula_of(counts) if counts else "Air",
                "density": round(_density_gcm3(definition.get("dens", 0.0), counts), 4),
                "thickness": round(float(definition.get("d", 0.0)), 4),
                "roughness": round(float(definition.get("sigma", 0.0)), 4),
            })
            name_pos[layer_name] = (seg_index, layer_index)
        if layer_defs:
            segments.append({
                "repetitions": max(1, int(stack["repetitions"])),
                "layers": layer_defs,
            })

    if not segments:
        raise ValueError("脚本里没有可展开的层")

    ambient_def = layers.get(sample.get("ambient") or "", {})
    substrate_def = layers.get(sample.get("substrate") or "", {})
    amb_counts = ambient_def.get("counts") or {}
    sub_counts = substrate_def.get("counts") or {}
    ambient = {
        "formula": _formula_of(amb_counts) if amb_counts else "Air",
        "density": 0.0012 if not amb_counts
        else round(_density_gcm3(ambient_def.get("dens", 0.0), amb_counts), 4),
    }
    substrate = {
        "formula": _formula_of(sub_counts) if sub_counts else "Si",
        "density": round(_density_gcm3(substrate_def.get("dens", 0.0), sub_counts), 4),
        "roughness": round(float(substrate_def.get("sigma", 0.0) or 0.0), 4),
    }

    # 参数表
    plugin_params = []
    for key, value, fit_flag, low, high in zip(param_keys, param_values,
                                               param_fit, param_min, param_max):
        if "." not in key:
            continue
        obj, attr = key.split(".", 1)
        if obj == "inst":
            inst_field = INST_MAP.get(attr)
            if not inst_field:
                continue
            plugin_params.append({
                "key": f"inst.{inst_field}",
                "label": INST_LABEL.get(inst_field, inst_field),
                "value": float(value),
                "fit": bool(fit_flag),
                "min": float(low),
                "max": float(high),
                "error": "",
            })
            continue
        if obj not in name_pos:
            continue
        field = FIELD_MAP.get(attr)
        if not field:
            continue
        seg_index, layer_index = name_pos[obj]
        if field == "density":
            counts = layers.get(obj, {}).get("counts") or {}
            molar = _molar_mass(counts)
            factor = (molar / NA_FACTOR) if molar > 0 else 0.0
            value = value * factor
            low = low * factor
            high = high * factor
        plugin_params.append({
            "key": f"seg.{seg_index}.layer.{layer_index}.{field}",
            "label": f"段{seg_index + 1}·层{layer_index + 1} {FIELD_LABEL[field]}",
            "value": float(value),
            "fit": bool(fit_flag),
            "min": float(low),
            "max": float(high),
            "error": "",
        })

    # GenX 保存的脚本是初始值，真实值在参数表里：用参数值覆盖层结构
    for item in plugin_params:
        parts = item["key"].split(".")
        if len(parts) < 5:
            continue
        seg_index = int(parts[1])
        layer_index = int(parts[3])
        field = parts[4]
        if 0 <= seg_index < len(segments):
            layer_list = segments[seg_index]["layers"]
            if 0 <= layer_index < len(layer_list):
                layer_list[layer_index][field] = round(float(item["value"]), 4)

    # 仪器
    coords = str(instrument.get("coords", "2θ"))
    x_unit = "q" if coords.strip().lower() in ("q", "qinv") else "2theta"
    restype = str(instrument.get("restype", "no conv"))
    resolution = {
        "type": "none" if restype.startswith("no conv") else
                ("relative" if "dx/x" in restype else "fast"),
        "width": float(instrument.get("res", 0.001)),
        "points": int(instrument.get("respoints", 5)),
        "spread": float(instrument.get("resintrange", 2.0)),
    }
    footprint = {
        "type": FOOT_MAP.get(str(instrument.get("footype", "no corr")), "none"),
        "beam_width": float(instrument.get("beamw", 0.05)),
        "sample_length": float(instrument.get("samplelen", 10.0)),
    }

    # 把仪器参数（拟合后的值）写回仪器配置
    inst_i0 = float(instrument.get("I0", 1.0))
    inst_ibkg = float(instrument.get("Ibkg", 0.0))
    inst_tthoff = float(instrument.get("tthoff", 0.0) or 0.0)
    for item in plugin_params:
        if not item["key"].startswith("inst."):
            continue
        field = item["key"].split(".", 1)[1]
        value = float(item["value"])
        if field == "res_width":
            resolution["width"] = value
        elif field == "beam_width":
            footprint["beam_width"] = value
        elif field == "sample_length":
            footprint["sample_length"] = value
        elif field == "i0":
            inst_i0 = value
        elif field == "ibkg":
            inst_ibkg = value
        elif field == "tthoff":
            inst_tthoff = value

    error_list = []
    if error is not None:
        for value in error:
            error_list.append(None if not np.isfinite(value) else float(value))
    else:
        error_list = [None] * len(x)

    dataset = {
        "id": "ds1",
        "name": file_path.stem,
        "x": [float(v) for v in x],
        "y": [float(v) for v in y],
        "error": error_list,
        "xUnit": x_unit,
    }

    return {
        "stack": {"ambient": ambient, "segments": segments, "substrate": substrate},
        "instrument": {
            "wavelength": float(instrument.get("wavelength", 1.54)),
            "i0": inst_i0,
            "ibkg": inst_ibkg,
            "tthoff": inst_tthoff,
            "resolution": resolution,
            "footprint": footprint,
        },
        "params": plugin_params,
        "datasets": [dataset],
        "activeId": "ds1",
        "algorithm": {"fom": fom if fom in
                      ("chi2bars", "log", "diff", "R1", "logR1") else "chi2bars"},
        "meta": {
            "source": str(file_path),
            "x_command": x_command,
            "genx_layers": len(layers),
            "genx_stacks": len(stacks),
            "segment_count": len(segments),
            "total_layers": sum(s["repetitions"] * len(s["layers"]) for s in segments),
            "has_x_raw": x_raw is not None,
            "params": len(plugin_params),
        },
    }
