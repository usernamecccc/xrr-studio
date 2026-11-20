"""项目保存 / 打开与数据导出。

默认目录：<用户主目录>/Documents/XRR
- 项目文件：JSON（含层结构、仪器、参数、数据集、拟合历史）
- 导出文件：CSV / TSV / TXT（曲线、参数表、拟合摘要）
"""

from __future__ import annotations

import json
import os
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


def resolve_dir(params: dict | None) -> Path:
    """解析保存目录：优先用调用方给的绝对路径，否则用默认目录。"""
    raw = ((params or {}).get("dir") or "").strip().strip('"')
    if not raw:
        return _ensure_dir()
    target = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not target.is_absolute():
        raise ValueError("保存位置必须是绝对路径，例如 D:\\XRR 结果")
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"无法创建目录 {target}：{exc}") from exc
    if not target.is_dir():
        raise ValueError(f"不是目录：{target}")
    return target


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def save_project(params: dict) -> dict:
    data = params.get("data")
    if not isinstance(data, dict):
        raise ValueError("缺少项目数据")
    name = _safe_name(params.get("name"), _stamp())
    directory = resolve_dir(params)
    path = directory / f"{name}.json"
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
    directory = resolve_dir(params)
    path = directory / f"{name}.{ext}"

    sep = "\t" if ext in ("tsv", "txt", "dat") else ","
    lines = []
    for row in rows:
        if isinstance(row, (list, tuple)):
            lines.append(sep.join(_format_cell(value) for value in row))
        else:
            lines.append(str(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(path), "rows": len(rows), "size": path.stat().st_size}


def pick_directory(params: dict) -> dict:
    """弹出系统文件夹选择框，返回用户选的目录（取消则返回空）。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise RuntimeError("当前 Python 缺少 tkinter，无法弹出目录选择框") from exc

    initial = ((params or {}).get("initial") or "").strip().strip('"')
    if initial:
        start = Path(os.path.expandvars(os.path.expanduser(initial)))
        if not start.is_dir():
            start = start.parent if start.parent.is_dir() else None
    else:
        start = None
    if start is None:
        start = DEFAULT_DIR if DEFAULT_DIR.exists() else Path.home() / "Documents"

    root_window = None
    try:
        root_window = tk.Tk()
        root_window.withdraw()
        root_window.attributes("-topmost", True)
        root_window.update()
        chosen = filedialog.askdirectory(
            parent=root_window,
            title="选择保存目录",
            initialdir=str(start),
            mustexist=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"无法打开目录选择框：{exc}") from exc
    finally:
        if root_window is not None:
            try:
                root_window.destroy()
            except Exception:  # noqa: BLE001
                pass

    return {"path": chosen or "", "cancelled": not bool(chosen)}


def reveal_path(params: dict) -> dict:
    """在文件管理器中打开文件所在目录（或目录本身）。"""
    raw = ((params or {}).get("path") or "").strip().strip('"')
    if not raw:
        raise ValueError("缺少路径")
    target = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not target.exists():
        raise FileNotFoundError(f"路径不存在：{target}")
    directory = target.parent if target.is_file() else target
    try:
        os.startfile(str(directory))  # Windows
    except AttributeError:
        import subprocess
        import sys
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(directory)])
    except OSError as exc:
        raise ValueError(f"无法打开文件夹：{exc}") from exc
    return {"opened": str(directory)}


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
