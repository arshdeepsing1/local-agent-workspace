"""Text from documents and the type and size of images, for attachments and read_file. No I/O except
read_file_text, which reads one file.

DOCX, XLSX and PPTX files are ZIP archives of XML: their text is read with zipfile and xml.etree within
limits against ZIP bombs (at most MAX_MEMBERS entries, MAX_MEMBER_BYTES per XML part read and
MAX_UNCOMPRESSED in all, counted as the parts are read, not from the sizes the archive claims), and a part
with a DOCTYPE or entity declaration is refused (no entity is ever expanded). PDF text needs the optional
pypdf package (the documents extra); without it a PDF raises PdfUnavailable. CSV, TSV, text and Markdown
files are read as UTF-8. Images (PNG, JPEG, GIF, WebP) are never decoded: only their type and pixel size
are read from their header (image_info).

Extracted text is plain text with one marker line per page, sheet or slide ("--- Page 3 ---"), at most
MAX_TEXT_CHARS characters; lines longer than LINE_CHARS are wrapped (at a space where there is one), so
any line fits the smallest read_file page (8 KB) even when each of its characters is escaped in JSON.
"""
import io
import re
import struct
import threading
import time
import zipfile
from collections import OrderedDict
from xml.etree import ElementTree

MAX_INPUT_BYTES = 20_000_000  # A document file (20 MB).
MAX_MEMBERS = 10_000
MAX_MEMBER_BYTES = 32_000_000
MAX_UNCOMPRESSED = 64_000_000
MAX_TEXT_CHARS = 2_000_000
MAX_SHEET_ROWS = 5_000
MAX_SHEET_COLUMNS = 200
MAX_PDF_PAGES = 2_000
PDF_SECONDS = 60.0  # Pages after this are left out (a note says so).
LINE_CHARS = 500
PDF_EXTRA = ("Reading PDF text needs the app's optional documents extra (the pypdf package): install it with "
             "python -m pip install -c constraints.txt -e './backend[documents]' and restart the app.")
DOCUMENT_FORMATS = {
    "pdf": ("PDF", "application/pdf"), "docx": ("Word document", "application/vnd.openxmlformats-officedocument."
                                                                 "wordprocessingml.document"),
    "xlsx": ("Excel workbook", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "pptx": ("PowerPoint presentation", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    "csv": ("CSV file", "text/csv"), "tsv": ("TSV file", "text/tab-separated-values"),
    "txt": ("text file", "text/plain"), "md": ("Markdown file", "text/markdown"),
}
EXTENSIONS = {"pdf": "pdf", "docx": "docx", "xlsx": "xlsx", "pptx": "pptx", "csv": "csv", "tsv": "tsv", "txt": "txt",
              "text": "txt", "log": "txt", "md": "md", "markdown": "md"}
# The binary formats read_file reads as extracted text; text formats it reads as they are.
BINARY_FORMATS = ("pdf", "docx", "xlsx", "pptx")
IMAGE_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
UNITS = {"pdf": "page", "xlsx": "sheet", "pptx": "slide"}


class DocumentError(ValueError):
    """A document the app cannot read; the message says why and never quotes its contents."""


class PdfUnavailable(DocumentError):
    """A PDF, and pypdf is not installed."""

    def __init__(self):
        super().__init__(PDF_EXTRA)


def document_format(name):
    """The document format of a file name ('pdf', 'docx', 'xlsx', 'pptx', 'csv', 'tsv', 'txt', 'md'), or None."""
    suffix = name.rsplit(".", 1)[-1].lower() if isinstance(name, str) and "." in name else ""
    return EXTENSIONS.get(suffix)


def format_label(fmt):
    return DOCUMENT_FORMATS.get(fmt, ("document", ""))[0]


def pdf_available():
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


# --- Images: type and pixel size from the header -------------------------------

def image_info(data):
    """{media_type, width, height} of a PNG, JPEG, GIF or WebP image, or None (another file, or a
    header whose size cannot be read). The image itself is never decoded."""
    size, kind = None, None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR" and len(data) >= 24:
        kind, size = "png", struct.unpack(">II", data[16:24])
    elif data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        kind, size = "gif", struct.unpack("<HH", data[6:10])
    elif data[:3] == b"\xff\xd8\xff":
        kind, size = "jpeg", _jpeg_size(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind, size = "webp", _webp_size(data)
    if kind is None or not size or min(size) < 1:
        return None
    return {"media_type": IMAGE_TYPES[kind], "width": int(size[0]), "height": int(size[1])}


def _jpeg_size(data):
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            return None
        marker = data[index + 1]
        if marker == 0xFF:  # Fill byte.
            index += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # No length field.
            index += 2
            continue
        length = struct.unpack(">H", data[index + 2:index + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # A start of frame.
            height, width = struct.unpack(">HH", data[index + 5:index + 9])
            return width, height
        if length < 2:
            return None
        index += 2 + length
    return None


def _webp_size(data):
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    if chunk == b"VP8X" and len(data) >= 30:
        return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    return None


# --- Documents ------------------------------------------------------------------

class _Text:
    """Extracted lines within MAX_TEXT_CHARS; truncated once something was left out."""

    def __init__(self):
        self.lines, self.size, self.limit, self.truncated = [], 0, MAX_TEXT_CHARS, False

    def add(self, line):
        if self.truncated:
            return False
        line = line.replace("\x00", "")
        while len(line) > LINE_CHARS:  # read_file pages by line.
            cut = line.rfind(" ", 0, LINE_CHARS)
            cut = cut if cut > LINE_CHARS // 2 else LINE_CHARS
            if not self._append(line[:cut]):
                return False
            line = line[cut:].lstrip(" ")
        return self._append(line)

    def _append(self, line):
        if self.size + len(line) + 1 > self.limit:
            self.truncated = True
            return False
        self.lines.append(line)
        self.size += len(line) + 1
        return True

    def text(self):
        return "\n".join(self.lines)


def extract(data, fmt):
    """{format, text, units, count, truncated, notes} of a document's bytes (fmt: document_format).
    units is 'page', 'sheet' or 'slide' (count of them) for PDF, XLSX and PPTX, else None. notes are short
    remarks for the reader (rows left out, pages without text). DocumentError when it cannot be read."""
    if len(data) > MAX_INPUT_BYTES:
        raise DocumentError(f"Documents can be at most {MAX_INPUT_BYTES // 1_000_000} MB.")
    out, notes = _Text(), []
    if fmt in ("csv", "tsv", "txt", "md"):
        count = None
        if b"\x00" in data[:8192]:
            raise DocumentError("This file is not text (it contains NUL bytes).")
        text = data.decode("utf-8-sig", errors="replace")
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if not out.add(line):
                break
    elif fmt == "pdf":
        count = _pdf(data, out, notes)
    elif fmt in ("docx", "xlsx", "pptx"):
        archive = _archive(data)
        try:
            count = {"docx": _docx, "xlsx": _xlsx, "pptx": _pptx}[fmt](archive, out, notes)
        except DocumentError:
            raise
        except Exception as exc:  # A damaged archive or XML part: never its contents in the message.
            raise DocumentError(f"This {format_label(fmt)} could not be read ({_reason(exc)}).") from None
    else:
        raise DocumentError("This file type is not a document the app reads.")
    if out.truncated:
        notes.append(f"The text was cut at {MAX_TEXT_CHARS:,} characters.")
    text = out.text().rstrip("\n")
    return {"format": fmt, "text": text, "units": UNITS.get(fmt), "count": count if fmt in UNITS else None,
            "truncated": out.truncated, "notes": notes}


def _reason(exc):
    if isinstance(exc, KeyError):
        return "a required part is missing"
    if isinstance(exc, ElementTree.ParseError):
        return "its XML is not valid"
    return "it is damaged or not this format"


class _Archive:
    """A ZIP archive whose parts are read within the bomb limits."""

    def __init__(self, data):
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            infos = self.zip.infolist()
        except (zipfile.BadZipFile, ValueError, EOFError, NotImplementedError):
            raise DocumentError("This file is not a valid Office document (not a ZIP archive).") from None
        if len(infos) > MAX_MEMBERS:
            raise DocumentError(f"This document has more than {MAX_MEMBERS:,} parts.")
        self.names = {info.filename: info for info in infos}
        self.read_bytes = 0

    def has(self, name):
        return name in self.names

    def read(self, name):
        info = self.names[name]  # KeyError: a required part is missing.
        limit = min(MAX_MEMBER_BYTES, MAX_UNCOMPRESSED - self.read_bytes)
        if info.file_size > limit:
            raise DocumentError("This document expands to more data than the app reads (a part is too large).")
        try:
            with self.zip.open(info) as source:
                data = source.read(limit + 1)  # Bounded even when the declared size is wrong.
        except (zipfile.BadZipFile, NotImplementedError, EOFError, RuntimeError, ValueError) as exc:
            raise DocumentError(f"A part of this document could not be read ({type(exc).__name__}).") from None
        if len(data) > limit:
            raise DocumentError("This document expands to more data than the app reads (a part is too large).")
        self.read_bytes += len(data)
        return data


def _archive(data):
    if data[:4] != b"PK\x03\x04":
        raise DocumentError("This file is not a valid Office document (not a ZIP archive).")
    return _Archive(data)


def _checked_xml(data):
    """data, refused when it declares a DOCTYPE or entities (so no entity is ever expanded, external or
    internal) or is not UTF-8 (where those checks could not see them)."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in data[:64]:
        raise DocumentError("This document has XML parts that are not UTF-8, which the app does not read.")
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:  # XML keywords are case-sensitive.
        raise DocumentError("This document contains a DOCTYPE or entity declaration, which the app does not read.")
    return data


def _xml(data):
    return ElementTree.fromstring(_checked_xml(data))


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _attribute(element, name):
    """An attribute by its local name, whatever its namespace."""
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return None


def _relation(element):
    """The relationship ID of a sheet or slide entry (r:id, in the relationships namespace; never a plain id)."""
    return next((value for key, value in element.attrib.items() if key.startswith("{") and _local(key) == "id"), "")


def _relationships(archive, name):
    """{id: target path within the archive} of a part's relationships file (folder/_rels/file.rels)."""
    folder, _, file = name.rpartition("/")
    rels = f"{folder}/_rels/{file}.rels" if folder else f"_rels/{file}.rels"
    if not archive.has(rels):
        return {}
    result = {}
    for item in _xml(archive.read(rels)):
        target, key = item.get("Target"), item.get("Id")
        if not target or not key or item.get("TargetMode") == "External":
            continue
        parts = (target.lstrip("/") if target.startswith("/") else f"{folder}/{target}" if folder else target).split("/")
        resolved = []
        for part in parts:
            if part == "..":
                if resolved:
                    resolved.pop()
            elif part and part != ".":
                resolved.append(part)
        result[key] = "/".join(resolved)
    return result


def _runs_text(element):
    """The text of a WordprocessingML or DrawingML paragraph, in document order: t, tab and line breaks."""
    pieces = []
    for node in element.iter():
        name = _local(node.tag)
        if name == "t" and node.text:
            pieces.append(node.text)
        elif name == "tab":
            pieces.append("\t")
        elif name in ("br", "cr"):
            pieces.append("\n")
    return "".join(pieces)


HEADING_STYLE = re.compile(r"^heading([1-6])$", re.IGNORECASE)


def _docx_paragraph(paragraph):
    text = _runs_text(paragraph)
    style = next((_attribute(node, "val") for node in paragraph.iter() if _local(node.tag) == "pStyle"), None)
    level = HEADING_STYLE.match(style.replace(" ", "")) if isinstance(style, str) else None
    if level and text.strip():
        return "#" * int(level.group(1)) + " " + text.strip()
    if isinstance(style, str) and style.lower() == "title" and text.strip():
        return "# " + text.strip()
    return text


def _docx_blocks(parent, out):
    for block in parent:
        name = _local(block.tag)
        if name == "p":
            for line in _docx_paragraph(block).split("\n"):
                if not out.add(line):
                    return
        elif name == "tbl":
            for row in (child for child in block if _local(child.tag) == "tr"):
                cells = [" ".join(_runs_text(paragraph).replace("\t", " ").replace("\n", " ").strip()
                                  for paragraph in cell.iter() if _local(paragraph.tag) == "p").strip()
                         for cell in row if _local(cell.tag) == "tc"]
                if not out.add("\t".join(cells)):
                    return
            out.add("")
        elif name in ("sdt", "sdtContent", "customXml", "smartTag"):
            _docx_blocks(block, out)


def _docx(archive, out, notes):
    root = _xml(archive.read("word/document.xml"))
    body = next((child for child in root if _local(child.tag) == "body"), root)
    _docx_blocks(body, out)
    return None


def _string_item(item):
    """The text of a shared string or an inline string: its t, or the t of each rich-text run (r).
    Phonetic runs (rPh) are reading aids, not the cell's text."""
    pieces = []
    for child in item:
        name = _local(child.tag)
        if name == "t":
            pieces.append(child.text or "")
        elif name == "r":
            pieces.extend(node.text or "" for node in child if _local(node.tag) == "t")
    return "".join(pieces)


def _shared_strings(archive):
    if not archive.has("xl/sharedStrings.xml"):
        return []
    strings = []
    for _, element in ElementTree.iterparse(io.BytesIO(_checked_xml(archive.read("xl/sharedStrings.xml")))):
        if _local(element.tag) == "si":
            strings.append(_string_item(element))
            element.clear()
    return strings


COLUMN = re.compile(r"^([A-Z]{1,3})(\d+)$")


def _column(reference):
    found = COLUMN.match(reference or "")
    if not found:
        return None
    number = 0
    for letter in found.group(1):
        number = number * 26 + ord(letter) - 64
    return number - 1


def _cell_text(cell, strings):
    kind = cell.get("t")
    value = next((node.text for node in cell if _local(node.tag) == "v"), None)
    if kind == "inlineStr":
        return "".join(_string_item(node) for node in cell if _local(node.tag) == "is")
    if kind == "s":
        try:
            return strings[int(value)]
        except (TypeError, ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if value == "1" else "FALSE"
    return value or ""


def _sheet(data, strings, out, name):
    """One worksheet as tab-separated rows; returns whether it was cut at the row or column limit."""
    rows = cut = 0
    for _, element in ElementTree.iterparse(io.BytesIO(_checked_xml(data))):
        if _local(element.tag) != "row":
            continue
        cells, position = {}, 0
        for cell in element:
            if _local(cell.tag) != "c":
                continue
            column = _column(cell.get("r"))
            position = column if column is not None else position
            if position < MAX_SHEET_COLUMNS:
                text = _cell_text(cell, strings).replace("\t", " ").replace("\r", " ").replace("\n", " ")
                if text:
                    cells[position] = text
            else:
                cut = 1
            position += 1
        element.clear()
        if not cells:
            continue
        rows += 1
        if rows > MAX_SHEET_ROWS:
            return True
        if not out.add("\t".join(cells.get(index, "") for index in range(max(cells) + 1))):
            return False
    return bool(cut)


def _xlsx(archive, out, notes):
    workbook = _xml(archive.read("xl/workbook.xml"))
    targets = _relationships(archive, "xl/workbook.xml")
    strings = _shared_strings(archive)
    sheets = [element for element in workbook.iter() if _local(element.tag) == "sheet"]
    count = 0
    for sheet in sheets:
        target = targets.get(_relation(sheet))
        if not target or not archive.has(target):
            continue
        count += 1
        hidden = " (hidden)" if sheet.get("state") in ("hidden", "veryHidden") else ""
        if not out.add(f"--- Sheet: {sheet.get('name') or count}{hidden} ---"):
            break
        if _sheet(archive.read(target), strings, out, sheet.get("name")):
            notes.append(f"Sheet {sheet.get('name') or count}: only the first {MAX_SHEET_ROWS:,} rows and "
                         f"{MAX_SHEET_COLUMNS} columns with values are included.")
        if not out.add(""):
            break
    return count


def _pptx(archive, out, notes):
    slides = []
    if archive.has("ppt/presentation.xml"):
        targets = _relationships(archive, "ppt/presentation.xml")
        for item in _xml(archive.read("ppt/presentation.xml")).iter():
            if _local(item.tag) == "sldId":
                target = targets.get(_relation(item))
                if target and archive.has(target):
                    slides.append(target)
    if not slides:  # No usable order: by file number.
        found = [(int(match.group(1)), name) for name in archive.names
                 if (match := re.fullmatch(r"ppt/slides/slide(\d+)\.xml", name))]
        slides = [name for _, name in sorted(found)]
    for number, name in enumerate(slides, 1):
        if not out.add(f"--- Slide {number} ---"):
            break
        root = _xml(archive.read(name))
        for paragraph in root.iter():
            if _local(paragraph.tag) == "p" and (text := _runs_text(paragraph).strip()):
                for line in text.split("\n"):
                    if not out.add(line):
                        break
        if not out.add(""):
            break
    return len(slides)


def _pdf(data, out, notes):
    if data[:1024].find(b"%PDF-") < 0:
        raise DocumentError("This file is not a valid PDF.")
    try:
        from pypdf import PdfReader
    except ImportError:
        raise PdfUnavailable() from None
    deadline = time.monotonic() + PDF_SECONDS
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocumentError("This PDF is password-protected, so its text cannot be read.")
        pages = reader.pages
        count, empty = len(pages), 0
        for index in range(min(count, MAX_PDF_PAGES)):
            if time.monotonic() > deadline:
                notes.append(f"Reading stopped after {index:,} of {count:,} pages (time limit).")
                break
            text = (pages[index].extract_text() or "").strip()
            if not out.add(f"--- Page {index + 1} ---"):
                break
            if not text:
                empty += 1
            for line in text.split("\n"):
                if not out.add(line):
                    break
            if out.truncated or not out.add(""):
                break
    except DocumentError:
        raise
    except Exception as exc:  # pypdf raises many kinds of errors on damaged files.
        raise DocumentError(f"This PDF could not be read ({type(exc).__name__}).") from None
    if count > MAX_PDF_PAGES:
        notes.append(f"Only the first {MAX_PDF_PAGES:,} of {count:,} pages are read.")
    if empty:
        notes.append(f"{empty:,} page{'s have' if empty != 1 else ' has'} no text layer (scanned pages need OCR, "
                     "which the app does not do).")
    return count


# --- read_file: extracted text of a project file, cached by size and time ------

_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()  # read_file runs in worker threads.
CACHE_ENTRIES = 4


def read_file_text(path, fmt):
    """extract() of a file (fmt: one of BINARY_FORMATS), cached for the last few files by path, size and
    modification time, so paging through a document extracts it once."""
    info = path.stat()
    if info.st_size > MAX_INPUT_BYTES:
        raise DocumentError(f"Documents up to {MAX_INPUT_BYTES // 1_000_000} MB are read as text; this one is "
                            f"{info.st_size / 1_000_000:.1f} MB.")
    key = (str(path), info.st_size, info.st_mtime_ns)
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    with path.open("rb") as source:
        data = source.read(MAX_INPUT_BYTES + 1)
    result = extract(data, fmt)
    with _CACHE_LOCK:
        _CACHE[key] = result
        while len(_CACHE) > CACHE_ENTRIES:
            _CACHE.popitem(last=False)
    return result
