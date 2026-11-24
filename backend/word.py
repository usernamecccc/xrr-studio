"""导出 Word 报告（.docx）：仪器条件、层结构、参数表、拟合结果、曲线图。

数据由页面传入（工程内容 + 曲线 PNG 的 base64），后端只负责拼装与写盘。
"""
from __future__ import annotations

import base64
import re
from datetime import datetime

import core
import docx_builder
import project

UNIT_LABEL = {"2theta": "2θ（度）", "theta": "θ（度）", "q": "q（Å⁻¹）"}
RES_LABEL = {"none": "不卷积", "fast": "固定宽度", "relative": "相对宽度"}
FOOT_LABEL = {"none": "不修正", "gauss": "高斯束", "square": "方形束"}


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt(value, digits=6) -> str:
    try:
        return f"{float(value):.{digits}g}"
    except (TypeError, ValueError):
        return "—"


def _decode_png(data_url) -> bytes | None:
    """把 data:image/png;base64,xxx 解成原始字节。"""
    if not data_url or not isinstance(data_url, str):
        return None
    raw = data_url.strip()
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    raw = re.sub(r"\s+", "", raw)
    try:
        blob = base64.b64decode(raw, validate=False)
    except Exception:  # noqa: BLE001
        return None
    if len(blob) < 24 or blob[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return blob


def _density_note(formula: str, density: float) -> str:
    bulk = core.bulk_density(formula)
    if not bulk:
        return "—"
    return f"{density / bulk * 100:.1f}%"


def _active_dataset(data: dict) -> dict | None:
    datasets = data.get("datasets") or []
    if not datasets:
        return None
    active = data.get("activeId")
    for ds in datasets:
        if ds.get("id") == active:
            return ds
    return datasets[0]


def build(data: dict, png: bytes | None, title: str, source: str = "") -> docx_builder.DocxBuilder:
    doc = docx_builder.DocxBuilder()
    doc.heading(title, 1)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    doc.paragraph(f"生成时间：{stamp}" + (f"　　数据源：{source}" if source else ""),
                  size_pt=9)

    stack = data.get("stack") or {}
    instrument = data.get("instrument") or {}
    params = data.get("params") or []
    fit_info = data.get("fit") or {}
    ds = _active_dataset(data)

    # 1 基本信息
    doc.heading("1　基本信息", 2)
    xs = (ds or {}).get("x") or []
    rows = [
        ["数据集", (ds or {}).get("name") or "—"],
        ["数据点数", str(len(xs))],
        ["横坐标", UNIT_LABEL.get((ds or {}).get("xUnit"), (ds or {}).get("xUnit") or "—")],
        ["横坐标范围", f"{min(xs):.4g} ～ {max(xs):.4g}" if xs else "—"],
        ["拟合状态", "已拟合" if fit_info else "未拟合"],
    ]
    doc.table(["项目", "内容"], rows, widths_cm=[4.0, 12.0], align=["center", None])

    # 2 仪器条件
    doc.heading("2　仪器条件", 2)
    res = instrument.get("resolution") or {}
    foot = instrument.get("footprint") or {}
    inst_rows = [
        ["波长 λ", _fmt(instrument.get("wavelength"), 5), "Å"],
        ["I₀", _fmt(instrument.get("i0"), 7), "—"],
        ["本底 I_bkg", _fmt(instrument.get("ibkg"), 7), "—"],
        ["分辨率类型", RES_LABEL.get(str(res.get("type", "none")).lower(), "—"), "—"],
        ["分辨率宽度 σ", _fmt(res.get("width")), "横坐标单位"],
        ["采样点数", str(res.get("points") or "—"), "pts"],
        ["积分范围", _fmt(res.get("spread"), 3), "±σ"],
        ["照射面积修正", FOOT_LABEL.get(str(foot.get("type", "none")).lower(), "—"), "—"],
        ["束宽", _fmt(foot.get("beam_width")), "mm"],
        ["样品长", _fmt(foot.get("sample_length")), "mm"],
        ["角度偏移 tthoff", _fmt(instrument.get("tthoff")), "°"],
    ]
    doc.table(["参数", "值", "单位"], inst_rows,
              widths_cm=[5.0, 7.0, 4.0], align=["center", "center", "center"])

    # 3 层结构
    doc.heading("3　层结构", 2)
    ambient = stack.get("ambient") or {}
    substrate = stack.get("substrate") or {}
    doc.paragraph(f"环境：{ambient.get('formula') or 'Air'}　"
                  f"{_fmt(ambient.get('density'), 4)} g/cm³", size_pt=9)
    layer_rows = []
    for si, seg in enumerate(stack.get("segments") or []):
        reps = int(_num(seg.get("repetitions"), 1))
        for li, layer in enumerate(seg.get("layers") or []):
            formula = layer.get("formula") or "—"
            density = _num(layer.get("density"))
            layer_rows.append([
                f"段{si + 1}（×{reps}）" if li == 0 else "",
                str(li + 1),
                formula,
                _fmt(layer.get("thickness"), 4),
                _fmt(density, 4),
                _density_note(formula, density),
                _fmt(layer.get("roughness"), 4),
            ])
    sub_formula = substrate.get("formula") or "—"
    sub_density = _num(substrate.get("density"))
    layer_rows.append(["衬底", "—", sub_formula, "—", _fmt(sub_density, 4),
                       _density_note(sub_formula, sub_density),
                       _fmt(substrate.get("roughness"), 4)])
    doc.table(["段", "层", "材料", "厚度 (Å)", "密度 (g/cm³)", "占 bulk", "界面宽度 (Å)"],
              layer_rows, widths_cm=[3.2, 1.2, 2.8, 2.4, 2.6, 2.0, 2.6],
              align=["center"] * 7)
    total_layers = sum(int(_num(s.get("repetitions"), 1)) * len(s.get("layers") or [])
                       for s in stack.get("segments") or [])
    doc.paragraph(f"展开后总层数：{total_layers}　"
                  f"（占 bulk = 拟合密度 / 该材料块体密度）", size_pt=9)

    # 4 拟合参数
    if params:
        doc.heading("4　拟合参数", 2)
        param_rows = []
        for p in params:
            err = p.get("error")
            param_rows.append([
                str(p.get("label") or p.get("key") or "—"),
                _fmt(p.get("value")),
                "是" if p.get("fit") else "否",
                _fmt(p.get("min")),
                _fmt(p.get("max")),
                str(err) if err not in (None, "", "-") else "—",
            ])
        doc.table(["参数", "值", "参与拟合", "最小", "最大", "误差"], param_rows,
                  widths_cm=[5.6, 3.0, 2.0, 2.0, 2.0, 1.4],
                  align=[None, "center", "center", "center", "center", "center"],
                  font_pt=8.5)

    # 5 拟合结果
    doc.heading("5　拟合结果", 2)
    algorithm = data.get("algorithm") or {}
    best = fit_info.get("best")
    fom_rows = [
        ["FOM 函数", str(algorithm.get("fom") or "—")],
        ["FOM 值", f"{float(best):.6f}" if isinstance(best, (int, float)) else "—"],
        ["迭代代数", str(fit_info.get("generation")) if fit_info.get("generation") is not None else "—"],
        ["自由参数数", str(len([p for p in params if p.get("fit")]))],
    ]
    doc.table(["项目", "内容"], fom_rows, widths_cm=[4.0, 12.0], align=["center", None])

    # 6 曲线
    if png:
        doc.heading("6　拟合曲线", 2)
        doc.image_bytes(png, width_cm=16.0)
        doc.caption("图 1　实测曲线与拟合曲线（纵轴对数）")

    return doc


def export_word(params: dict) -> dict:
    payload = params or {}
    data = payload.get("data")
    if not isinstance(data, dict) or "stack" not in data:
        raise ValueError("缺少工程数据（stack）")
    png = _decode_png(payload.get("image"))
    title = (payload.get("title") or "XRR 拟合报告").strip() or "XRR 拟合报告"
    source = (payload.get("source") or "").strip()

    doc = build(data, png, title, source)
    blob = doc.to_bytes()

    directory = project.resolve_dir(payload)
    name = project._safe_name(payload.get("name"), project._stamp())
    path = directory / f"{name}.docx"
    path.write_bytes(blob)
    return {
        "path": str(path),
        "name": path.stem,
        "size": path.stat().st_size,
        "has_image": png is not None,
    }
