"""项目保存 / 打开与数据导出。

默认目录：<用户主目录>/Documents/XRR
- 项目文件：JSON（含层结构、仪器、参数、数据集、拟合历史）
- 导出文件：CSV / TSV / TXT（曲线、参数表、拟合摘要）
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

DEFAULT_DIR = Path.home() / "Documents" / "XRR"

_BAD_CHARS = '\\/:*?"<>|'


def _safe_name(name: str, fallback: str) -> str:
    cleaned = "".join(ch for ch in (name or "") if ch not in _BAD_CHARS).strip()
    return cleaned or fallback


def _ensure_dir() -> Path:
    DEFAULT_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_DIR


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def save_project(params: dict) -> dict:
    data = params.get("data")
    if not isinstance(data, dict):
        raise ValueError("缺少项目数据")
    name = _safe_name(params.get("name"), _stamp())
    path = _ensure_dir() / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"path": str(path), "name": path.stem, "size": path.stat().st_size}


def load_project(params: dict) -> dict:
    raw = (params.get("path") or "").strip()
    if not raw:
        raise ValueError("缺少文件路径")
    path = Path(raw)
    if not path.exists():
        candidate = DEFAULT_DIR / raw
        if candidate.exists():
            path = candidate
        elif (DEFAULT_DIR / f"{raw}.json").exists():
            path = DEFAULT_DIR / f"{raw}.json"
        else:
            raise FileNotFoundError(f"找不到文件：{raw}")
    if path.suffix.lower() != ".json":
        raise ValueError("项目文件必须是 .json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "stack" not in data:
        raise ValueError("不是有效的 XRR 项目文件（缺少 stack）")
    return {"path": str(path), "data": data}


def list_projects(params: dict | None = None) -> dict:
    _ensure_dir()
    files = []
    for item in sorted(DEFAULT_DIR.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
        stat = item.stat()
        files.append({
            "name": item.stem,
            "path": str(item),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return {"dir": str(DEFAULT_DIR), "files": files}


def export_data(params: dict) -> dict:
    rows = params.get("rows") or []
    if not rows:
        raise ValueError("没有可导出的数据")
    kind = _safe_name(params.get("kind"), "data")
    name = _safe_name(params.get("name"), f"{kind}-{_stamp()}")
    ext = (params.get("ext") or "csv").lower()
    if ext not in ("csv", "tsv", "txt", "dat"):
        ext = "csv"
    path = _ensure_dir() / f"{name}.{ext}"

    sep = "\t" if ext in ("tsv", "txt", "dat") else ","
    lines = []
    for row in rows:
        if isinstance(row, (list, tuple)):
            lines.append(sep.join(_format_cell(value) for value in row))
        else:
            lines.append(str(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(path), "rows": len(rows), "size": path.stat().st_size}


def _format_cell(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if value == 0.0:
            return "0"
        if abs(value) < 1e-4 or abs(value) >= 1e5:
            return f"{value:.6e}"
        return f"{value:.6f}"
    return str(value)
