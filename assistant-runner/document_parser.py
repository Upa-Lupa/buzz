"""Bounded, offline document parsing. No macros, links or formulas are executed."""

import base64
import io
import json
import math
import sys
import warnings
import zipfile

from PIL import Image, ImageOps

MAX_TEXT = 8000  # characters; Korean UTF-8 still fits the agent text budget
MAX_PARTS = 400
MAX_IMAGE_BYTES = 1500000
Image.MAX_IMAGE_PIXELS = 24_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)


def image_result(image):
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((2000, 2000))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    if out.tell() > MAX_IMAGE_BYTES:
        raise ValueError("Rendered image exceeds output limit")
    return {"image": base64.b64encode(out.getvalue()).decode(), "mime": "image/jpeg"}


def checked_zip(data):
    archive = zipfile.ZipFile(io.BytesIO(data))
    entries = archive.infolist()
    if len(entries) > 2000 or sum(i.file_size for i in entries) > 80 * 1024 * 1024:
        raise ValueError("Office archive exceeds entry/decompression limit")
    for item in entries:
        if item.flag_bits & 1 or item.file_size > 25 * 1024 * 1024:
            raise ValueError("Encrypted/oversized Office archive entry")
        if "vbaproject" in item.filename.lower():
            raise ValueError(
                "Macro-enabled documents are not supported; save as docx/xlsx"
            )
    return archive


def text_result(text, offset):
    if not 0 <= offset <= len(text):
        raise ValueError("Invalid text offset")
    return {
        "text": text[offset : offset + MAX_TEXT],
        "text_length": len(text),
        "next_offset": offset + MAX_TEXT if offset + MAX_TEXT < len(text) else None,
    }


def office_parts(data, kind):
    """Ordered Word blocks or bounded worksheet row blocks, plus embedded images."""
    archive = checked_zip(data)
    parts = []
    if kind == "docx":
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        doc = Document(io.BytesIO(data))
        blocks = []
        for block in doc.iter_inner_content():
            if isinstance(block, Paragraph):
                blocks.append(block.text)
            elif isinstance(block, Table):
                blocks.append(
                    "[Table]\n"
                    + "\n".join(
                        " | ".join(cell.text for cell in row.cells)
                        for row in block.rows
                    )
                )
        for index, section in enumerate(doc.sections):
            blocks.extend(
                [
                    f"[Section {index + 1} header] "
                    + "\n".join(p.text for p in section.header.paragraphs),
                    f"[Section {index + 1} footer] "
                    + "\n".join(p.text for p in section.footer.paragraphs),
                ]
            )
        text = "\n\n".join(blocks)
        if len(text) > 2_000_000:
            raise ValueError("Word document exceeds text limit")
        parts.append(("Word paragraphs/tables (not a paginated layout render)", text))
        prefix = "word/media/"
    else:
        import openpyxl

        book = openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=False, keep_links=False
        )
        cached = openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=True, keep_links=False
        )
        try:
            if len(book.worksheets) > 50:
                raise ValueError("Workbook exceeds 50-sheet limit")
            for sheet in book:
                rows, columns = sheet.max_row or 0, sheet.max_column or 0
                if rows * columns > 200_000 or columns > 200:
                    raise ValueError("Worksheet exceeds 200,000 cells / 200 columns")
                lines = []
                for i, (row, values) in enumerate(
                    zip(sheet.iter_rows(), cached[sheet.title].iter_rows()), 1
                ):
                    cells = []
                    for cell, value in zip(row, values):
                        if cell.value is not None:
                            entry = f"{cell.coordinate}={cell.value}"
                            if cell.data_type == "f":
                                entry += f" [cached={value.value}; not recalculated]"
                            cells.append(entry)
                    lines.append(" | ".join(cells))
                    if i % 100 == 0 or i == rows:
                        parts.append(
                            (
                                f"Sheet {sheet.title} ({sheet.sheet_state}) rows {i - len(lines) + 1}-{i}",
                                "\n".join(lines),
                            )
                        )
                        lines = []
                if not rows:
                    parts.append((f"Sheet {sheet.title}", "[Empty sheet]"))
        finally:
            book.close()
            cached.close()
        prefix = "xl/media/"
    for name in archive.namelist():
        if name.startswith(prefix) and not name.endswith("/"):
            parts.append(
                (f"Embedded image {name} (placement not inferred)", archive.read(name))
            )
    return parts


def parse(data, filename, action="inspect", part=1, offset=0):
    ext = filename.rsplit(".", 1)[-1].lower()
    if data.startswith(b"%PDF-"):
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(data)
        try:
            count = len(doc)
            if count > MAX_PARTS:
                raise ValueError("PDF exceeds 400 pages")
            if action == "inspect":
                return {
                    "format": "pdf",
                    "parts": [
                        {"part": i + 1, "label": f"Page {i + 1}", "viewable": True}
                        for i in range(count)
                    ],
                }
            if not 1 <= part <= count:
                raise ValueError("Page out of range")
            page = doc[part - 1]
            try:
                if action == "view":
                    width, height = page.get_size()
                    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
                        raise ValueError("Invalid PDF page dimensions")
                    bitmap = page.render(scale=min(2, 2000 / max(width, height)))
                    try:
                        return image_result(bitmap.to_pil())
                    finally:
                        bitmap.close()
                textpage = page.get_textpage()
                try:
                    if textpage.count_chars() > 200_000:
                        raise ValueError("PDF page exceeds text limit")
                    return text_result(textpage.get_text_range(), offset)
                finally:
                    textpage.close()
            finally:
                page.close()
        finally:
            doc.close()
    if ext in ("docx", "xlsx"):
        parts = office_parts(data, ext)
    elif ext == "xls":
        import xlrd

        book = xlrd.open_workbook(file_contents=data, on_demand=True)
        parts = []
        try:
            for sheet in book.sheets():
                if sheet.nrows * sheet.ncols > 200_000 or sheet.ncols > 200:
                    raise ValueError("Worksheet exceeds cell limit")
                for start in range(0, max(1, sheet.nrows), 100):
                    lines = [
                        f"row {r + 1}: "
                        + " | ".join(str(v) for v in sheet.row_values(r))
                        for r in range(start, min(start + 100, sheet.nrows))
                    ]
                    parts.append(
                        (
                            f"Sheet {sheet.name} rows {start + 1}-{min(start + 100, sheet.nrows)} (stored values; Excel dates may be serials)",
                            "\n".join(lines),
                        )
                    )
        finally:
            book.release_resources()
    elif ext in ("png", "jpg", "jpeg", "webp", "gif", "tif", "tiff", "bmp"):
        with Image.open(io.BytesIO(data)) as image:
            count = getattr(image, "n_frames", 1)
            if count > MAX_PARTS:
                raise ValueError("Image exceeds frame limit")
            if action == "inspect":
                return {
                    "format": "image",
                    "parts": [
                        {
                            "part": i + 1,
                            "label": f"Image/frame {i + 1}",
                            "viewable": True,
                        }
                        for i in range(count)
                    ],
                }
            if action != "view":
                return {
                    "text": "Image has no text layer. Use view_attachment to read it visually.",
                    "next_offset": None,
                }
            if not 1 <= part <= count:
                raise ValueError("Frame out of range")
            image.seek(part - 1)
            return image_result(image)
    else:
        raise ValueError(
            "Unsupported format. Use PDF, DOCX, XLSX, XLS or PNG/JPEG/WebP/GIF/TIFF/BMP. Legacy DOC and encrypted files require conversion."
        )
    if len(parts) > MAX_PARTS:
        raise ValueError("Document exceeds 400-part limit")
    if action == "inspect":
        return {
            "format": ext,
            "parts": [
                {"part": i + 1, "label": title, "viewable": isinstance(content, bytes)}
                for i, (title, content) in enumerate(parts)
            ],
        }
    if not 1 <= part <= len(parts):
        raise ValueError("Part out of range")
    title, content = parts[part - 1]
    if isinstance(content, bytes):
        if action != "view":
            return {"text": "Embedded image: use view_attachment.", "next_offset": None}
        with Image.open(io.BytesIO(content)) as image:
            return image_result(image)
    if action == "view":
        raise ValueError(
            "This part is structured text, not an image. Use read_attachment. For exact Word/Excel print layout, supply PDF."
        )
    return {"label": title, **text_result(content, offset)}


if __name__ == "__main__":
    # Separate parser process: bounded CPU, address space, output, and no secrets.
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (3 * 1024 * 1024, 3 * 1024 * 1024))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    try:
        data = sys.stdin.buffer.read(25 * 1024 * 1024 + 1)
        if len(data) > 25 * 1024 * 1024:
            raise ValueError("Input exceeds 25 MiB")
        result = parse(
            data, sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
        )
        output = json.dumps(result, ensure_ascii=False).encode()
        if len(output) > 2 * 1024 * 1024:
            raise ValueError("Parser output exceeds 2 MiB")
        sys.stdout.buffer.write(output)
    except Exception as exc:  # noqa: BLE001 - parser boundary propagates every failure via nonzero exit
        # No document content or credentials in parser failure messages.
        sys.stdout.write(
            json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:250]}"})
        )
        sys.exit(1)
