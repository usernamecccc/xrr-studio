"""极简 docx 生成器：只用标准库（zipfile + 字符串）。

输出 OOXML Transitional，WPS 2016+ 与 Word 2007+ 均可打开。
刻意避开 w14/w15 等 Word 专有命名空间，保证 WPS 兼容。
"""
from __future__ import annotations

import io
import zipfile

EMU_PER_CM = 360000
HALF_PT = 2


def esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def png_size(raw: bytes):
    """从 PNG 头读宽高。"""
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是有效的 PNG 数据")
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")


class DocxBuilder:
    """按顺序堆段落 / 表格 / 图片，最后 save() 或 to_bytes()。"""

    def __init__(self, body_font="宋体", latin_font="Times New Roman", size_pt=10.5):
        self.body_font = body_font
        self.latin_font = latin_font
        self.size_pt = size_pt
        self._parts: list[str] = []
        self._images: list[bytes] = []
        self._pic_id = 0

    # ---------------------------------------------------------------- 内部
    def _rpr(self, bold=False, size_pt=None, italic=False) -> str:
        size = int(round((size_pt or self.size_pt) * HALF_PT))
        bits = [
            f'<w:rFonts w:ascii="{self.latin_font}" w:hAnsi="{self.latin_font}" '
            f'w:eastAsia="{self.body_font}"/>',
            f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>',
        ]
        if bold:
            bits.append("<w:b/>")
        if italic:
            bits.append("<w:i/>")
        return "<w:rPr>" + "".join(bits) + "</w:rPr>"

    def _para(self, runs_xml, align=None, space_before=0, space_after=0) -> str:
        props = []
        if align:
            props.append(f'<w:jc w:val="{align}"/>')
        props.append(f'<w:spacing w:before="{space_before}" w:after="{space_after}"/>')
        return "<w:p><w:pPr>" + "".join(props) + "</w:pPr>" + runs_xml + "</w:p>"

    # ---------------------------------------------------------------- 内容
    def heading(self, text, level=1):
        size = {1: 16, 2: 14, 3: 12}.get(level, 11)
        run = (f"<w:r>{self._rpr(bold=True, size_pt=size)}"
               f'<w:t xml:space="preserve">{esc(text)}</w:t></w:r>')
        self._parts.append(self._para(run, space_before=240, space_after=120))
        return self

    def paragraph(self, text, bold=False, size_pt=None, align=None, italic=False,
                  space_after=60):
        run = (f"<w:r>{self._rpr(bold=bold, size_pt=size_pt, italic=italic)}"
               f'<w:t xml:space="preserve">{esc(text)}</w:t></w:r>')
        self._parts.append(self._para(run, align=align, space_after=space_after))
        return self

    def caption(self, text):
        run = (f"<w:r>{self._rpr(size_pt=9)}"
               f'<w:t xml:space="preserve">{esc(text)}</w:t></w:r>')
        self._parts.append(self._para(run, align="center", space_before=60, space_after=180))
        return self

    def spacer(self, pts=6):
        self._parts.append(f'<w:p><w:pPr><w:spacing w:after="{pts * 20}"/></w:pPr></w:p>')
        return self

    def table(self, headers, rows, widths_cm=None, align=None, header_bold=True,
              font_pt=None):
        cols = len(headers)
        font_pt = font_pt or (self.size_pt - 0.5)
        if widths_cm is None:
            widths_cm = [16.0 / cols] * cols
        total = sum(widths_cm)
        widths_dxa = [int(w * 567) for w in widths_cm]

        def cell(text, width_dxa, bold=False, center=False):
            run = (f"<w:r>{self._rpr(bold=bold, size_pt=font_pt)}"
                   f'<w:t xml:space="preserve">{esc(text)}</w:t></w:r>')
            para = self._para(run, align="center" if center else None)
            return (f'<w:tc><w:tcPr><w:tcW w:w="{width_dxa}" w:type="dxa"/>'
                    f'<w:vAlign w:val="center"/></w:tcPr>{para}</w:tc>')

        grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths_dxa)
        border = (
            "<w:tblBorders>"
            + "".join(f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
                      for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
            + "</w:tblBorders>"
        )
        tbl_pr = (f'<w:tblPr><w:tblW w:w="{int(total * 567)}" w:type="dxa"/>'
                  f"{border}<w:tblLayout w:type=\"fixed\"/></w:tblPr>")

        out = ["<w:tbl>", tbl_pr, "<w:tblGrid>", grid, "</w:tblGrid>"]
        out.append("<w:tr><w:trPr><w:tblHeader/></w:trPr>"
                   + "".join(cell(h, widths_dxa[i], bold=header_bold, center=True)
                             for i, h in enumerate(headers))
                   + "</w:tr>")
        for row in rows:
            cells = []
            for i in range(cols):
                value = row[i] if i < len(row) else ""
                center = bool(align and i < len(align) and align[i] == "center")
                cells.append(cell(value, widths_dxa[i], center=center))
            out.append("<w:tr>" + "".join(cells) + "</w:tr>")
        out.append("</w:tbl>")
        self._parts.append("".join(out))
        self.spacer(4)
        return self

    def image_bytes(self, raw: bytes, width_cm=16.0):
        """嵌入内存中的 PNG 数据，宽度固定，高度按原图比例。"""
        w_px, h_px = png_size(raw)
        cx = int(width_cm * EMU_PER_CM)
        cy = int(cx * h_px / max(w_px, 1))
        self._pic_id += 1
        pid = self._pic_id
        rel_id = f"rId{100 + pid}"
        self._images.append(raw)
        self._parts.append(f"""
<w:p><w:pPr><w:jc w:val="center"/><w:spacing w:before="120" w:after="60"/></w:pPr><w:r><w:drawing>
<wp:inline distT="0" distB="0" distL="0" distR="0">
<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>
<wp:docPr id="{pid}" name="Picture {pid}"/>
<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>
<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
<pic:pic><pic:nvPicPr><pic:cNvPr id="{pid}" name="image{pid}.png"/><pic:cNvPicPr/></pic:nvPicPr>
<pic:blipFill><a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>
<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>
</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>""")
        return self

    # ---------------------------------------------------------------- 输出
    def to_bytes(self) -> bytes:
        sect = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                '<w:pgMar w:top="1440" w:right="1304" w:bottom="1440" w:left="1304" '
                'w:header="851" w:footer="992" w:gutter="0"/></w:sectPr>')
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            "<w:body>" + "".join(self._parts) + sect + "</w:body></w:document>"
        )
        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
        )
        root_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
        )
        doc_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{100 + i}" Type="http://schemas.openxmlformats.org/'
                f'officeDocument/2006/relationships/image" Target="media/image{i}.png"/>'
                for i in range(1, len(self._images) + 1))
            + "</Relationships>"
        )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", content_types)
            zf.writestr("_rels/.rels", root_rels)
            zf.writestr("word/document.xml", document)
            zf.writestr("word/_rels/document.xml.rels", doc_rels)
            for i, raw in enumerate(self._images, start=1):
                zf.writestr(f"word/media/image{i}.png", raw)
        return buf.getvalue()

    def save(self, path):
        from pathlib import Path
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.to_bytes())
        return path
