from __future__ import annotations

import csv
import io
import json
import math
import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Sequence
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

MAX_EXCEL_TEXT = 32767
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
URL_RE = re.compile(r"https?://[^\s<>\"|]+", re.IGNORECASE)


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def _column_index(cell_ref: str) -> int:
    letters = re.sub(r"[^A-Z]", "", cell_ref.upper())
    out = 0
    for char in letters:
        out = out * 26 + (ord(char) - 64)
    return out


def _clean_sheet_name(name: str) -> str:
    cleaned = re.sub(r"[\[\]\*:/\\?]", " ", name).strip() or "Datos"
    return cleaned[:31]


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple, dict)):
        value = json.dumps(value, ensure_ascii=False)
    text = "".join(
        char
        for char in str(value)
        if char in "\t\n\r" or "\x20" <= char <= "\ud7ff" or "\ue000" <= char <= "\ufffd"
    )
    if len(text) > MAX_EXCEL_TEXT:
        text = text[: MAX_EXCEL_TEXT - 3] + "..."
    return text


def _number(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    return None


def _hyperlink_target(value: object) -> str | None:
    match = URL_RE.search(_text(value))
    if not match:
        return None
    return match.group(0).rstrip(").,;]}'\"")


def _inline_cell(row: int, col: int, value: object, style: int | None = None) -> str:
    ref = f"{_column_name(col)}{row}"
    style_attr = f' s="{style}"' if style is not None else ""
    number = _number(value)
    if number is not None:
        return f'<c r="{ref}"{style_attr}><v>{number}</v></c>'
    text = _text(value)
    if text == "":
        return f'<c r="{ref}"{style_attr}/>'
    escaped = escape(text, {'"': "&quot;"})
    return f'<c r="{ref}" t="inlineStr"{style_attr}><is><t xml:space="preserve">{escaped}</t></is></c>'


def write_xlsx(
    rows: Iterable[Dict[str, object]],
    fieldnames: Sequence[str],
    path: str | Path,
    sheet_name: str = "Datos",
) -> Path:
    rows = list(rows)
    fieldnames = list(fieldnames)
    if not fieldnames:
        fieldnames = ["value"]

    path = Path(path)
    if path.suffix.lower() != ".xlsx":
        path = path.with_suffix(".xlsx")
    path.parent.mkdir(parents=True, exist_ok=True)

    row_count = len(rows) + 1
    col_count = len(fieldnames)
    last_ref = f"{_column_name(col_count)}{max(row_count, 1)}"
    dimension = f"A1:{last_ref}"

    widths: List[float] = []
    for field in fieldnames:
        max_len = len(field)
        for row in rows[:2000]:
            max_len = max(max_len, len(_text(row.get(field))))
        widths.append(min(max(max_len + 2, 10), 60))

    sheet_rows = []
    header_cells = [_inline_cell(1, idx, field, style=1) for idx, field in enumerate(fieldnames, start=1)]
    sheet_rows.append(f'<row r="1">{"".join(header_cells)}</row>')
    hyperlinks = []
    worksheet_rels = []
    for row_idx, row in enumerate(rows, start=2):
        cells = []
        for col_idx, field in enumerate(fieldnames, start=1):
            value = row.get(field)
            target = _hyperlink_target(value)
            style = 2 if target else None
            cells.append(_inline_cell(row_idx, col_idx, value, style=style))
            if target:
                cell_ref = f"{_column_name(col_idx)}{row_idx}"
                rel_id = f"rId{len(worksheet_rels) + 1}"
                hyperlinks.append(f'<hyperlink ref="{cell_ref}" r:id="{rel_id}"/>')
                worksheet_rels.append(
                    '<Relationship '
                    f'Id="{rel_id}" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
                    f'Target="{escape(target, {"\"": "&quot;"})}" '
                    'TargetMode="External"/>'
                )
        sheet_rows.append(f'<row r="{row_idx}">{"".join(cells)}</row>')

    cols_xml = "".join(
        f'<col min="{idx}" max="{idx}" width="{width:.1f}" customWidth="1"/>'
        for idx, width in enumerate(widths, start=1)
    )
    hyperlinks_xml = f'<hyperlinks>{"".join(hyperlinks)}</hyperlinks>' if hyperlinks else ""
    sheet_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{SHEET_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <dimension ref="{dimension}"/>
  <sheetViews>
    <sheetView workbookViewId="0">
      <pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>
      <selection pane="bottomLeft" activeCell="A2" sqref="A2"/>
    </sheetView>
  </sheetViews>
  <sheetFormatPr defaultRowHeight="15"/>
  <cols>{cols_xml}</cols>
  <sheetData>{"".join(sheet_rows)}</sheetData>
  <autoFilter ref="{dimension}"/>
  {hyperlinks_xml}
</worksheet>'''

    workbook_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{SHEET_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="{escape(_clean_sheet_name(sheet_name), {'"': '&quot;'})}" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>'''

    workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''

    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''

    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>'''

    styles = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{SHEET_NS}">
  <fonts count="3">
    <font><sz val="11"/><name val="Calibri"/></font>
    <font><b/><sz val="11"/><name val="Calibri"/></font>
    <font><u/><sz val="11"/><color rgb="FF0563C1"/><name val="Calibri"/></font>
  </fonts>
  <fills count="3">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFECECEC"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="3">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
    <xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
  <dxfs count="0"/>
  <tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/>
</styleSheet>'''

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        if worksheet_rels:
            worksheet_rels_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + "".join(worksheet_rels)
                + "</Relationships>"
            )
            zf.writestr("xl/worksheets/_rels/sheet1.xml.rels", worksheet_rels_xml)
        zf.writestr("xl/styles.xml", styles)
    return path


def write_table(rows: Iterable[Dict[str, object]], fieldnames: Sequence[str], path: str | Path, sheet_name: str = "Datos") -> Path:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            f.write("sep=;\n")
            writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return path
    return write_xlsx(rows, fieldnames, path, sheet_name=sheet_name)


def read_csv_dicts(path: str | Path) -> List[Dict[str, str]]:
    text = Path(path).read_text(encoding="utf-8-sig")
    delimiter = ","
    if text.lower().startswith("sep="):
        first_line, _, rest = text.partition("\n")
        marker = first_line[4:].strip()
        delimiter = marker[:1] or ","
        text = rest
    raw_rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if raw_rows and len(raw_rows[0]) == 1 and "," in raw_rows[0][0]:
        # Algunas reconstrucciones historicas quedaron como una fila CSV entera
        # dentro de una sola celda quoteada. Rehidratar antes de convertir.
        repaired = "\n".join(row[0] for row in raw_rows)
        return list(csv.DictReader(io.StringIO(repaired)))
    if not raw_rows:
        return []
    headers = raw_rows[0]
    return [
        {header: row[idx] if idx < len(row) else "" for idx, header in enumerate(headers)}
        for row in raw_rows[1:]
    ]


def _load_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    try:
        raw = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    ns = {"x": SHEET_NS}
    values = []
    for item in root.findall("x:si", ns):
        values.append("".join(t.text or "" for t in item.findall(".//x:t", ns)))
    return values


def read_xlsx_dicts(path: str | Path) -> List[Dict[str, str]]:
    with zipfile.ZipFile(path) as zf:
        shared = _load_shared_strings(zf)
        sheet = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
    ns = {"x": SHEET_NS}
    table: List[List[str]] = []
    for row in sheet.findall(".//x:sheetData/x:row", ns):
        values: List[str] = []
        for cell in row.findall("x:c", ns):
            col_idx = _column_index(cell.attrib.get("r", "A1"))
            while len(values) < col_idx:
                values.append("")
            ctype = cell.attrib.get("t", "")
            if ctype == "inlineStr":
                value = "".join(t.text or "" for t in cell.findall(".//x:t", ns))
            elif ctype == "s":
                raw = cell.findtext("x:v", default="", namespaces=ns)
                value = shared[int(raw)] if raw.isdigit() and int(raw) < len(shared) else raw
            else:
                value = cell.findtext("x:v", default="", namespaces=ns)
            values[col_idx - 1] = value
        table.append(values)

    if not table:
        return []
    headers = table[0]
    return [
        {header: row[idx] if idx < len(row) else "" for idx, header in enumerate(headers)}
        for row in table[1:]
        if any(row)
    ]


def read_table(path: str | Path) -> List[Dict[str, str]]:
    path = Path(path)
    if path.suffix.lower() == ".xlsx":
        return read_xlsx_dicts(path)
    return read_csv_dicts(path)


def csv_to_xlsx(csv_path: str | Path, xlsx_path: str | Path | None = None) -> Path:
    csv_path = Path(csv_path)
    rows = read_csv_dicts(csv_path)
    fieldnames = list(rows[0].keys()) if rows else []
    if not fieldnames:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            fieldnames = next(reader, [])
    return write_xlsx(rows, fieldnames, xlsx_path or csv_path.with_suffix(".xlsx"), sheet_name=csv_path.stem)
