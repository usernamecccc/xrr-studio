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

import sys
import traceback

import core
import fit
import hgx
import project

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
]


if serve is not None:
    serve("XRR 拟合工作台", _TOOLS)
