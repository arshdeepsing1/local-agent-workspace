"""Text from attached and project documents (documents.py): DOCX, XLSX and PPTX read with zipfile and
xml.etree within limits, PDF through the optional pypdf package, image headers, and read_file on documents."""
import io
import struct
import sys
import types
import zipfile
import zlib

import pytest

from local_agent import documents
from local_agent.documents import DocumentError, PdfUnavailable, document_format, extract, image_info
from local_agent.tools import WorkspaceTools

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
RELS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def archive(parts, compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as target:
        for name, text in parts.items():
            target.writestr(name, text if isinstance(text, bytes) else text.encode("utf-8"))
    return buffer.getvalue()


def docx(body):
    return archive({"[Content_Types].xml": "<Types/>",
                    "word/document.xml": f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{body}</w:body></w:document>'})


def paragraph(text, style=None):
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{props}<w:r><w:t>{text}</w:t></w:r></w:p>"


def rels(*targets):
    items = "".join(f'<Relationship Id="rId{index}" Type="x" Target="{target}"/>' for index, target in enumerate(targets, 1))
    return f"<Relationships {RELS}>{items}</Relationships>"


def xlsx(sheets, strings=(), hidden=()):
    """sheets: {name: [[cell xml, ...], ...]} (rows of <c> elements)."""
    entries = "".join(f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"'
                      f'{" state=\"hidden\"" if name in hidden else ""}/>' for index, name in enumerate(sheets, 1))
    parts = {"xl/workbook.xml": f"<workbook {S} {R}><sheets>{entries}</sheets></workbook>",
             "xl/_rels/workbook.xml.rels": rels(*(f"worksheets/sheet{index}.xml" for index in range(1, len(sheets) + 1))),
             "xl/sharedStrings.xml": f"<sst {S}>" + "".join(f"<si><t>{text}</t></si>" for text in strings) + "</sst>"}
    for index, rows in enumerate(sheets.values(), 1):
        data = "".join(f'<row r="{number}">{"".join(cells)}</row>' for number, cells in enumerate(rows, 1))
        parts[f"xl/worksheets/sheet{index}.xml"] = f"<worksheet {S}><sheetData>{data}</sheetData></worksheet>"
    return archive(parts)


def slide(*texts):
    shapes = "".join(f"<p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>" for text in texts)
    return f"<p:sld {P} {A}><p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>"


def test_formats_come_from_the_file_name():
    assert [document_format(name) for name in ("a.PDF", "b.docx", "c.xlsx", "d.pptx", "e.csv", "f.tsv", "g.txt",
                                               "h.md", "i.markdown", "j.log", "k.doc", "l", "m.png", None)] == [
        "pdf", "docx", "xlsx", "pptx", "csv", "tsv", "txt", "md", "md", "txt", None, None, None, None]


def test_docx_paragraphs_headings_tabs_breaks_and_tables():
    body = (paragraph("Quarterly report", "Title") + paragraph("Results", "Heading1") +
            "<w:p><w:r><w:t>Revenue</w:t><w:tab/><w:t>rose</w:t><w:br/><w:t>again</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Region</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Sales</w:t></w:r></w:p></w:tc></w:tr>"
            "<w:tr><w:tc><w:p><w:r><w:t>North</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>12</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            + paragraph("Detail", "Heading 2") + "<w:sdt><w:sdtContent>" + paragraph("Inside a control") + "</w:sdtContent></w:sdt>")
    result = extract(docx(body), "docx")
    assert result["text"] == ("# Quarterly report\n# Results\nRevenue\trose\nagain\nRegion\tSales\nNorth\t12\n\n## Detail\n"
                              "Inside a control")
    assert result["units"] is None and result["count"] is None and not result["truncated"] and result["notes"] == []


def test_xlsx_sheets_as_tab_separated_rows():
    rows = [['<c r="A1" t="s"><v>0</v></c>', '<c r="B1" t="s"><v>1</v></c>'],
            ['<c r="A2" t="inlineStr"><is><t>North</t></is></c>', '<c r="C2"><v>12.5</v></c>', '<c r="D2" t="b"><v>1</v></c>'],
            [],
            ['<c r="B4" t="s"><v>9</v></c>']]
    result = extract(xlsx({"Sales": rows, "Notes": [['<c r="A1" t="inlineStr"><is><r><t>rich </t></r><r><t>text</t></r></is></c>']]},
                          strings=("Region", "Total"), hidden=("Notes",)), "xlsx")
    assert result["text"] == ("--- Sheet: Sales ---\nRegion\tTotal\nNorth\t\t12.5\tTRUE\n\n"
                              "--- Sheet: Notes (hidden) ---\nrich text")
    assert (result["units"], result["count"]) == ("sheet", 2)


def test_xlsx_rows_and_columns_are_bounded(monkeypatch):
    monkeypatch.setattr(documents, "MAX_SHEET_ROWS", 2)
    monkeypatch.setattr(documents, "MAX_SHEET_COLUMNS", 2)
    rows = [[f'<c r="{column}{number}"><v>{number}</v></c>' for column in "ABC"] for number in range(1, 5)]
    result = extract(xlsx({"Big": rows}), "xlsx")
    assert result["text"] == "--- Sheet: Big ---\n1\t1\n2\t2"
    assert result["notes"] == ["Sheet Big: only the first 2 rows and 2 columns with values are included."]


def test_pptx_slides_in_presentation_order():
    parts = {"ppt/presentation.xml": f"<p:presentation {P} {R}><p:sldIdLst><p:sldId id='256' r:id='rId2'/>"
                                     f"<p:sldId id='257' r:id='rId1'/></p:sldIdLst></p:presentation>",
             "ppt/_rels/presentation.xml.rels": rels("slides/slide1.xml", "slides/slide2.xml"),
             "ppt/slides/slide1.xml": slide("Second"), "ppt/slides/slide2.xml": slide("Opening", "Agenda")}
    result = extract(archive(parts), "pptx")
    assert result["text"] == "--- Slide 1 ---\nOpening\nAgenda\n\n--- Slide 2 ---\nSecond"
    assert (result["units"], result["count"]) == ("slide", 2)
    # Without a presentation part, slides go by their file number.
    unordered = archive({"ppt/slides/slide10.xml": slide("Ten"), "ppt/slides/slide2.xml": slide("Two")})
    assert extract(unordered, "pptx")["text"] == "--- Slide 1 ---\nTwo\n\n--- Slide 2 ---\nTen"


@pytest.mark.parametrize("prolog", ['<!DOCTYPE w:document [<!ENTITY a "aaaa">]>',
                                    '<!DOCTYPE w:document SYSTEM "file:///etc/hostname">',
                                    '<!ENTITY a "aaaa">'])
def test_doctype_and_entity_declarations_are_refused(prolog):
    data = archive({"word/document.xml": f'<?xml version="1.0"?>{prolog}<w:document {W}><w:body>{paragraph("&a;")}</w:body></w:document>'})
    with pytest.raises(DocumentError, match="DOCTYPE or entity declaration"):
        extract(data, "docx")


def test_xml_that_is_not_utf8_is_refused():
    text = f'<?xml version="1.0" encoding="UTF-16"?><w:document {W}><w:body/></w:document>'
    with pytest.raises(DocumentError, match="not UTF-8"):
        extract(archive({"word/document.xml": text.encode("utf-16")}), "docx")


def test_zip_bombs_are_refused_by_what_is_read(monkeypatch):
    monkeypatch.setattr(documents, "MAX_UNCOMPRESSED", 200_000)
    bomb = docx(paragraph("x" * 300_000))
    assert len(bomb) < 5_000  # Small on disk, large when expanded.
    with pytest.raises(DocumentError, match="expands to more data than the app reads"):
        extract(bomb, "docx")
    monkeypatch.setattr(documents, "MAX_MEMBERS", 3)
    many = archive({f"part{index}.xml": "<a/>" for index in range(5)})
    with pytest.raises(DocumentError, match="more than 3 parts"):
        extract(many, "docx")


def test_a_part_whose_declared_size_is_wrong_is_still_bounded(monkeypatch):
    monkeypatch.setattr(documents, "MAX_MEMBER_BYTES", 1_000)
    data = bytearray(docx(paragraph("y" * 50_000)))
    # Make the central directory and the local header both claim a tiny uncompressed size.
    for signature, offset in ((b"PK\x01\x02", 24), (b"PK\x03\x04", 22)):
        start = 0
        while (start := data.find(signature, start)) >= 0:
            if signature == b"PK\x01\x02" or data[start + 30:start + 30 + 17] == b"word/document.xml":
                data[start + offset:start + offset + 4] = struct.pack("<I", 10)
            start += 4
    with pytest.raises(DocumentError):
        extract(bytes(data), "docx")


def test_damaged_files_name_no_contents():
    with pytest.raises(DocumentError, match="not a valid Office document"):
        extract(b"secret words, not a zip", "docx")
    with pytest.raises(DocumentError, match="a required part is missing"):
        extract(archive({"other.xml": "<a/>"}), "docx")
    with pytest.raises(DocumentError, match="its XML is not valid") as raised:
        extract(archive({"word/document.xml": "<w:document>secret unclosed"}), "docx")
    assert "secret" not in str(raised.value)
    with pytest.raises(DocumentError, match="at most 20 MB"):
        extract(b"x" * (documents.MAX_INPUT_BYTES + 1), "txt")


def test_text_files_long_lines_and_the_text_limit(monkeypatch):
    result = extract("﻿name,total\r\nNorth,12\r\n".encode("utf-8"), "csv")
    assert result["text"] == "name,total\nNorth,12" and result["units"] is None
    with pytest.raises(DocumentError, match="NUL bytes"):
        extract(b"a\x00b", "txt")
    wrapped = extract((("word " * 300) + "\n" + "z" * 1200).encode(), "txt")["text"].split("\n")
    assert all(len(line) <= documents.LINE_CHARS for line in wrapped) and len(wrapped) == 6
    assert " ".join(wrapped[:3]).split() == ["word"] * 300 and "".join(wrapped[3:]) == "z" * 1200
    monkeypatch.setattr(documents, "MAX_TEXT_CHARS", 30)
    cut = extract(b"line one\nline two\nline three\nline four\n", "txt")
    assert cut["text"] == "line one\nline two\nline three" and cut["truncated"]
    assert cut["notes"] == ["The text was cut at 30 characters."]


def test_pdf_needs_the_documents_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)  # Not installed.
    assert not documents.pdf_available()
    with pytest.raises(PdfUnavailable) as raised:
        extract(b"%PDF-1.7\n...", "pdf")
    assert "documents extra" in str(raised.value) and "backend[documents]" in str(raised.value)
    with pytest.raises(DocumentError, match="not a valid PDF"):
        extract(b"plain text", "pdf")


class FakePage:
    def __init__(self, text, error=None):
        self.text, self.error = text, error

    def extract_text(self):
        if self.error:
            raise self.error
        return self.text


def fake_pypdf(monkeypatch, pages, encrypted=False, password_ok=False):
    module = types.ModuleType("pypdf")

    class PdfReader:
        def __init__(self, stream):
            assert stream.read(5) == b"%PDF-"
            self.pages, self.is_encrypted = pages, encrypted

        def decrypt(self, password):
            return password_ok
    module.PdfReader = PdfReader
    monkeypatch.setitem(sys.modules, "pypdf", module)


def test_pdf_pages_with_pypdf(monkeypatch):
    fake_pypdf(monkeypatch, [FakePage("Title page\nIntro"), FakePage(""), FakePage("  Closing  ")])
    result = extract(b"%PDF-1.7 fake", "pdf")
    assert result["text"] == "--- Page 1 ---\nTitle page\nIntro\n\n--- Page 2 ---\n\n\n--- Page 3 ---\nClosing"
    assert (result["units"], result["count"]) == ("page", 3)
    assert result["notes"] == ["1 page has no text layer (scanned pages need OCR, which the app does not do)."]
    monkeypatch.setattr(documents, "MAX_PDF_PAGES", 2)
    assert "Only the first 2 of 3 pages are read." in extract(b"%PDF-1.7 fake", "pdf")["notes"]


def test_pdf_errors(monkeypatch):
    fake_pypdf(monkeypatch, [], encrypted=True)
    with pytest.raises(DocumentError, match="password-protected"):
        extract(b"%PDF-1.7", "pdf")
    fake_pypdf(monkeypatch, [FakePage("", error=zlib.error("secret detail"))])
    with pytest.raises(DocumentError, match=r"could not be read \(error\)") as raised:
        extract(b"%PDF-1.7", "pdf")
    assert "secret" not in str(raised.value)


def png(width, height):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"


def jpeg(width, height):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    frame = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, height, width) + b"\x03" + b"\x00" * 9
    return b"\xff\xd8" + app0 + frame + b"\xff\xd9"


def test_image_types_and_sizes_from_their_headers():
    assert image_info(png(1280, 720)) == {"media_type": "image/png", "width": 1280, "height": 720}
    assert image_info(b"GIF89a" + struct.pack("<HH", 64, 32) + b"\x00" * 8) == {
        "media_type": "image/gif", "width": 64, "height": 32}
    assert image_info(jpeg(800, 600)) == {"media_type": "image/jpeg", "width": 800, "height": 600}
    vp8x = b"RIFF" + b"\x00" * 4 + b"WEBPVP8X" + b"\x00" * 8 + (1919).to_bytes(3, "little") + (1079).to_bytes(3, "little")
    assert image_info(vp8x) == {"media_type": "image/webp", "width": 1920, "height": 1080}
    bits = (99) | (49 << 14)
    vp8l = b"RIFF" + b"\x00" * 4 + b"WEBPVP8L" + b"\x00" * 4 + b"\x2f" + bits.to_bytes(4, "little")
    assert image_info(vp8l) == {"media_type": "image/webp", "width": 100, "height": 50}
    for other in (b"", b"%PDF-1.7", png(0, 10), b"\xff\xd8\xff\xe0" + b"\x00" * 20, b"RIFF....WEBPVP8 "):
        assert image_info(other) is None


def test_read_file_pages_through_extracted_text(tmp_path):
    rows = [[f'<c r="A{number}" t="inlineStr"><is><t>row {number}</t></is></c>'] for number in range(1, 301)]
    (tmp_path / "data.xlsx").write_bytes(xlsx({"Rows": rows}))
    tools = WorkspaceTools(str(tmp_path))
    first = tools.read_file_range("data.xlsx", 1, 3)
    assert first["content"] == "1: --- Sheet: Rows ---\n2: row 1\n3: row 2"
    assert first["extracted"] == "text extracted from this Excel workbook (1 sheet)"
    assert (first["end_line"], first["next_line"], first["truncated"]) == (3, 4, True)
    later = tools.read_file_range("data.xlsx", 300, 10)
    assert later["content"] == "300: row 299\n301: row 300" and later["next_line"] is None and not later["truncated"]
    tail = tools.read_file_range("data.xlsx", tail_lines=2)
    assert tail["content"] == "300: row 299\n301: row 300" and tail["tail"] and (tail["start_line"], tail["end_line"]) == (300, 301)
    # A page holds what fits its byte limit; next_line continues.
    wide = [[f'<c r="A{number}" t="inlineStr"><is><t>row {number} {"data " * 10}</t></is></c>'] for number in range(1, 301)]
    (tmp_path / "wide.xlsx").write_bytes(xlsx({"Rows": wide}))
    small = tools.read_file_range("wide.xlsx", 1, 2000, page_bytes=8_000)
    assert small["truncated"] and small["next_line"] == small["end_line"] + 1 < 301
    assert len(small["content"]) < 8_000
    # Changed on disk: extracted again (the cache is keyed by size and modification time).
    (tmp_path / "data.xlsx").write_bytes(xlsx({"Other": [['<c r="A1" t="inlineStr"><is><t>new</t></is></c>']]}))
    assert tools.read_file_range("data.xlsx")["content"] == "1: --- Sheet: Other ---\n2: new"


def test_read_file_on_a_pdf_without_the_extra_and_a_damaged_document(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.7\n")
    (tmp_path / "broken.docx").write_bytes(b"PK\x03\x04 not really")
    tools = WorkspaceTools(str(tmp_path))
    with pytest.raises(ValueError, match="documents extra"):
        tools.read_file_range("paper.pdf")
    with pytest.raises(ValueError, match="not a valid Office document"):
        tools.read_file_range("broken.docx")
    fake_pypdf(monkeypatch, [FakePage("Abstract")])
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.7\n%changed\n")
    result = tools.read_file_range("paper.pdf")
    assert result["content"] == "1: --- Page 1 ---\n2: Abstract" and result["extracted"] == "text extracted from this PDF (1 page)"
