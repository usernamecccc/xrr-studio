"""XRR 拟合工作台 —— 页面后端（Python）。

暴露给插件页面的方法（对应 plugin.json 的 backend.ui_methods）：
  simulate      单次仿真
  materials     元素与常用材料清单
  sld_profile   SLD 深度剖面
  fit_start     启动差分进化拟合（后台线程，立即返回 job_id）
  fit_status    查询拟合进度（代数、最优 FOM、收敛历史）
  fit_stop      请求停止拟合

后端只做数值计算，不读写用户文件；数据由页面读取后传入。
"""

from __future__ import annotations

import math
import sys
import traceback

import core
import fit
import hgx
import project
import word

try:
    from wgallop_plugin import serve
except ImportError:  # 便于脱离宿主单独调试
    serve = None


def _ok(payload):
    return {"ok": True, "data": payload}


def _fail(exc: Exception):
    return {
        "ok": False,
        "error": f"{type(exc).__name__}: {exc}",
        "trace": traceback.format_exc(limit=4),
    }


def simulate(params, caller=None):
    try:
        return _ok(core.simulate(params or {}))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def materials(params=None, caller=None):
    try:
        return _ok(core.materials())
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def sld_profile(params, caller=None):
    try:
        return _ok(core.sld_profile(params or {}))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def guess_unit(params, caller=None):
    """按 FOM 自动识别横坐标单位：对 q / 2theta / theta 各仿真一次，取 FOM 最小者。

    用于文本数据导入后校正单位猜测（XRR 里 θ 与 2θ 数值范围重叠，光看数值猜不准）。
    """
    try:
        payload = params or {}
        stack = payload.get("stack") or {}
        inst = payload.get("instrument") or {}
        data = payload.get("data") or {}
        x = data.get("x") or []
        y = data.get("y") or []
        if not x or not y:
            raise ValueError("缺少数据")
        fom_name = (payload.get("algorithm") or {}).get("fom") or "log"
        if fom_name not in fit.FOM_NAMES:
            fom_name = "log"

        results = {}
        for unit in ("q", "2theta", "theta"):
            try:
                sim = core.simulate({
                    "wavelength": float(inst.get("wavelength") or 1.54),
                    "x": x,
                    "x_unit": unit,
                    "ambient": stack.get("ambient") or {},
                    "substrate": stack.get("substrate") or {},
                    "segments": stack.get("segments") or [],
                    "i0": float(inst.get("i0", 1.0)),
                    "ibkg": float(inst.get("ibkg", 0.0)),
                    "resolution": inst.get("resolution") or {},
                    "footprint": inst.get("footprint") or {},
                    "tthoff": float(inst.get("tthoff", 0.0) or 0.0),
                })["intensity"]
                value = float(fit.compute_fom(fom_name, y, sim, None, n_params=0))
                results[unit] = value if math.isfinite(value) else None
            except Exception:  # noqa: BLE001
                results[unit] = None

        usable = {k: v for k, v in results.items() if v is not None}
        if not usable:
            raise ValueError("三种单位都无法完成仿真")
        best = min(usable, key=usable.get)
        return _ok({"fom": results, "best": best, "best_fom": usable[best],
                    "fom_name": fom_name})
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def fit_start(params, caller=None):
    try:
        return _ok(fit.start_fit(params or {}))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def fit_status(params, caller=None):
    try:
        job_id = (params or {}).get("job_id") or ""
        return _ok(fit.fit_status(job_id))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def fit_stop(params, caller=None):
    try:
        job_id = (params or {}).get("job_id") or ""
        return _ok(fit.fit_stop(job_id))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def diagnose(params=None, caller=None):
    """返回后端运行环境信息，用于排查依赖与路径问题。"""
    info = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
        "sys_path": [p for p in sys.path[:6]],
    }
    for name in ("numpy", "h5py", "scipy"):
        try:
            module = __import__(name)
            info[name] = getattr(module, "__version__", "ok")
        except Exception as exc:  # noqa: BLE001
            info[name] = f"缺失（{type(exc).__name__}）"
    try:
        import core
        info["vendor"] = str(core._VENDOR)
        info["vendor_exists"] = core._VENDOR.is_dir()
        info["vendor_in_path"] = str(core._VENDOR) in sys.path
    except Exception as exc:  # noqa: BLE001
        info["vendor"] = f"无法确定：{exc}"
    try:
        info["elements"] = len(core.ELEMENTS.weights)
    except Exception:  # noqa: BLE001
        info["elements"] = 0
    return _ok(info)


def import_hgx(params, caller=None):
    try:
        payload = params or {}
        data = payload.get("data")
        if data:
            import base64
            raw = base64.b64decode(data)
            return _ok(hgx.load_bytes(raw, payload.get("name") or ""))
        return _ok(hgx.load(payload.get("path") or ""))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def pick_directory(params, caller=None):
    try:
        return _ok(project.pick_directory(params or {}))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def reveal_path(params, caller=None):
    try:
        return _ok(project.reveal_path(params or {}))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def save_project(params, caller=None):
    try:
        return _ok(project.save_project(params or {}))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def load_project(params, caller=None):
    try:
        return _ok(project.load_project(params or {}))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def list_projects(params=None, caller=None):
    try:
        return _ok(project.list_projects(params or {}))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def export_data(params, caller=None):
    try:
        return _ok(project.export_data(params or {}))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


def export_word(params, caller=None):
    try:
        return _ok(word.export_word(params or {}))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


_SIM_SCHEMA = {
    "type": "object",
    "properties": {
        "wavelength": {"type": "number", "description": "波长 [Å]"},
        "q": {"type": "array", "items": {"type": "number"}},
        "x": {"type": "array", "items": {"type": "number"}},
        "x_unit": {"type": "string", "enum": ["q", "2theta"]},
        "ambient": {"type": "object"},
        "substrate": {"type": "object"},
        "layers": {"type": "array", "items": {"type": "object"}},
        "i0": {"type": "number"},
        "ibkg": {"type": "number"},
        "resolution": {"type": "object"},
        "footprint": {"type": "object"},
    },
    "additionalProperties": True,
}

_FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "stack": {"type": "object"},
        "instrument": {"type": "object"},
        "data": {"type": "object"},
        "params": {"type": "array", "items": {"type": "object"}},
        "algorithm": {"type": "object"},
    },
    "additionalProperties": True,
}

_TOOLS = [
    {
        "name": "simulate",
        "description": "计算 X 射线镜面反射率曲线（Parratt 递推 + Nevot-Croce 粗糙度）。",
        "inputSchema": _SIM_SCHEMA,
        "execute": simulate,
    },
    {
        "name": "materials",
        "description": "返回可用元素列表与常用材料密度预设。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True},
        "execute": materials,
    },
    {
        "name": "sld_profile",
        "description": "返回层结构的 SLD 深度剖面。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True},
        "execute": sld_profile,
    },
    {
        "name": "guess_unit",
        "description": "按 FOM 自动识别横坐标单位（q / 2theta / theta），返回三者 FOM 与最优者。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "stack": {"type": "object"},
                "instrument": {"type": "object"},
                "data": {"type": "object"},
                "algorithm": {"type": "object"},
            },
            "additionalProperties": True,
        },
        "execute": guess_unit,
    },
    {
        "name": "fit_start",
        "description": "启动差分进化拟合，返回 job_id；用 fit_status 轮询进度。",
        "inputSchema": _FIT_SCHEMA,
        "execute": fit_start,
    },
    {
        "name": "fit_status",
        "description": "查询拟合作业的代数、最优 FOM、收敛历史与终态参数。",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": True,
        },
        "execute": fit_status,
    },
    {
        "name": "fit_stop",
        "description": "请求停止正在运行的拟合作业。",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": True,
        },
        "execute": fit_stop,
    },
    {
        "name": "diagnose",
        "description": "返回后端 Python 解释器与依赖状态，用于排查环境问题。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True},
        "execute": diagnose,
    },
    {
        "name": "import_hgx",
        "description": "读取 GenX 项目文件（.hgx）：传 path 走文件路径，传 data 走 base64 内容。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "项目文件的完整路径"},
                "data": {"type": "string", "description": "base64 编码的文件内容（浏览器选择文件时用）"},
                "name": {"type": "string", "description": "文件名，仅用于显示"},
            },
            "additionalProperties": True,
        },
        "execute": import_hgx,
    },
    {
        "name": "pick_directory",
        "description": "弹出系统文件夹选择框，返回用户选择的目录路径。",
        "inputSchema": {
            "type": "object",
            "properties": {"initial": {"type": "string", "description": "起始目录"}},
            "additionalProperties": True,
        },
        "execute": pick_directory,
    },
    {
        "name": "reveal_path",
        "description": "在系统文件管理器中打开文件所在目录。",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": True,
        },
        "execute": reveal_path,
    },
    {
        "name": "save_project",
        "description": "把当前工程保存为 JSON 项目文件（默认目录 ~/Documents/XRR）。",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "data": {"type": "object"}},
            "required": ["data"],
            "additionalProperties": True,
        },
        "execute": save_project,
    },
    {
        "name": "load_project",
        "description": "按路径或项目名读取 JSON 项目文件。",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": True,
        },
        "execute": load_project,
    },
    {
        "name": "list_projects",
        "description": "列出默认目录下最近的项目文件。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True},
        "execute": list_projects,
    },
    {
        "name": "export_data",
        "description": "把二维数据写成 CSV/TSV/TXT 文件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "name": {"type": "string"},
                "ext": {"type": "string"},
                "rows": {"type": "array"},
            },
            "required": ["rows"],
            "additionalProperties": True,
        },
        "execute": export_data,
    },
    {
        "name": "export_word",
        "description": "导出 Word 报告（.docx）：仪器条件、层结构、参数表、拟合结果与曲线图。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "data": {"type": "object", "description": "工程内容"},
                "image": {"type": "string", "description": "曲线 PNG 的 data URL（可选）"},
                "name": {"type": "string"},
                "dir": {"type": "string"},
                "title": {"type": "string"},
                "source": {"type": "string"},
            },
            "required": ["data"],
            "additionalProperties": True,
        },
        "execute": export_word,
    },
]


if serve is not None:
    serve("XRR 拟合工作台", _TOOLS)
