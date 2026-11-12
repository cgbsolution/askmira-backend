"""A ZIP of an employee's documents is opened and every file in it checked.

The user (2026-11-12): HR attaches one ZIP of a person's papers, at most 5 MB,
and "no miscellaneous file can go to our storage, or any vulnerability".
Each test is one way a ZIP can carry something other than plain documents.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from PIL import Image
from pypdf import PdfWriter

from app.services.documents import zip_scan
from app.services.documents.zip_scan import ZipRejectedError, check_zip
from app.services.storage.sniff import FileTooLargeError

LIMIT = 10 * 1024 * 1024


def _png(colour: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 48), colour).save(out, "PNG")
    return out.getvalue()


def _jpeg(*, exif_gps: bool = False) -> bytes:
    image = Image.new("RGB", (64, 48), (30, 90, 160))
    out = io.BytesIO()
    if exif_gps:
        exif = Image.Exif()
        exif[0x8825] = {1: "N", 2: (28.0, 32.0, 0.0)}  # GPSInfo: a phone camera's position
        image.save(out, "JPEG", exif=exif)
    else:
        image.save(out, "JPEG")
    return out.getvalue()


def _pdf(
    *, javascript: bool = False, attachment: bool = False, password: str | None = None
) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    if javascript:
        writer.add_js("app.alert('hi');")
    if attachment:
        writer.add_attachment("payload.exe", b"MZ\x90\x00")
    if password is not None:
        writer.encrypt(user_password=password, owner_password="owner-secret")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _zip(entries: dict[str, bytes], *, method: int = zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", method) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _refused(data: bytes) -> list[str]:
    with pytest.raises(ZipRejectedError) as refused:
        check_zip(data, max_file_bytes=LIMIT)
    return refused.value.problems


def _names(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return sorted(archive.namelist())


# ── What goes through ────────────────────────────────────────────────────────


def test_a_set_of_plain_documents_is_kept_as_one_rebuilt_zip():
    data = _zip(
        {
            "PAN card.pdf": _pdf(),
            "certificates/Degree.PNG": _png(),
            "Aadhaar.jpeg": _jpeg(),
            "__MACOSX/._Aadhaar.jpeg": b"resource fork",
            ".DS_Store": b"finder",
            "certificates/": b"",
        }
    )
    checked = check_zip(data, max_file_bytes=LIMIT)
    assert sorted(checked.files) == ["Aadhaar.jpeg", "PAN card.pdf", "certificates/Degree.PNG"]
    # The stored ZIP is a new one holding exactly those files: the OS clutter is gone.
    assert _names(checked.data) == sorted(checked.files)
    assert checked.uploaded_size == len(data)


def test_an_image_is_saved_again_so_what_was_hidden_in_it_is_gone():
    hidden = b"<script>steal()</script>"
    data = _zip({"photo.jpg": _jpeg(exif_gps=True) + hidden})
    checked = check_zip(data, max_file_bytes=LIMIT)
    with zipfile.ZipFile(io.BytesIO(checked.data)) as archive:
        stored = archive.read("photo.jpg")
    assert hidden not in stored
    assert 0x8825 not in Image.open(io.BytesIO(stored)).getexif()  # no GPS position


def test_a_pdf_locked_only_against_editing_is_accepted():
    # Bank statements often carry an owner password and no password to open.
    checked = check_zip(_zip({"statement.pdf": _pdf(password="")}), max_file_bytes=LIMIT)
    assert checked.files == ("statement.pdf",)


def test_a_png_named_jpg_is_stored_under_its_real_type_and_names_never_collide():
    data = _zip({"scan.jpg": _png(), "scan.png": _png((1, 2, 3)), "SCAN.png": _png((4, 5, 6))})
    checked = check_zip(data, max_file_bytes=LIMIT)
    # Names compare without case, as on Windows, so SCAN.png cannot overwrite scan.png.
    assert checked.files == ("scan.png", "scan (2).png", "SCAN (3).png")


# ── What is refused ──────────────────────────────────────────────────────────


def test_a_program_renamed_pdf_and_any_other_type_are_refused():
    problems = _refused(
        _zip(
            {
                "offer.pdf": b"MZ\x90\x00\x03" + b"\x00" * 64,
                "setup.exe": b"MZ\x90\x00",
                "notes.txt": b"hello",
                "page.pdf": b"<html><script>x()</script></html>",
                "fine.pdf": _pdf(),
            }
        )
    )
    text = "\n".join(problems)
    assert "offer.pdf is a program" in text
    assert "setup.exe is a .exe file" in text
    assert "notes.txt is a .txt file" in text
    assert "page.pdf is a web page or script" in text
    assert "fine.pdf" not in text
    assert len(problems) == 4


def test_a_zip_inside_the_zip_is_refused():
    inner = _zip({"a.pdf": _pdf()})
    problems = _refused(_zip({"more.pdf": inner}))
    assert "ZIP (or an Office file) inside the ZIP" in problems[0]


@pytest.mark.parametrize(
    ("pdf", "what"),
    [
        (_pdf(javascript=True), "JavaScript"),
        (_pdf(attachment=True), "a file hidden inside it"),
    ],
)
def test_a_pdf_that_can_run_or_carry_something_is_refused(pdf, what):
    problems = _refused(_zip({"letter.pdf": pdf}))
    assert what in problems[0]
    assert "which can run on the computer that opens it" in problems[0]


def test_javascript_disguised_in_a_name_is_still_found():
    # "/J#61vaScript" is "/JavaScript" with one letter escaped: a raw search misses
    # it, the walk over every object does not (the /JS key gives it away).
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R /OpenAction 4 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>\nendobj\n"
        b"4 0 obj\n<< /S /J#61vaScript /J#53 (app.alert(1)) >>\nendobj\n"
    )
    xref_at = len(body)
    offsets = [body.index(f"{n} 0 obj".encode()) for n in range(1, 5)]
    xref = b"xref\n0 5\n0000000000 65535 f \n" + b"".join(
        f"{offset:010d} 00000 n \n".encode() for offset in offsets
    )
    pdf = body + xref + b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref_at
    assert b"/JavaScript" not in pdf
    problems = _refused(_zip({"form.pdf": pdf}))
    assert "JavaScript" in problems[0]


def test_a_password_protected_pdf_or_data_after_its_end_is_refused():
    problems = _refused(
        _zip({"locked.pdf": _pdf(password="1234"), "tail.pdf": _pdf() + b"PK\x03\x04hidden"})
    )
    text = "\n".join(problems)
    assert "locked.pdf is locked with a password" in text
    assert "tail.pdf has data hidden after the end of the PDF" in text


def test_a_broken_image_is_refused():
    problems = _refused(_zip({"scan.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 40}))
    assert "damaged or malformed image" in problems[0]


def test_a_zip_bomb_is_not_unpacked():
    # 8 MB of zeros (under the 10 MB file limit) packs to a few KB: far past 100x.
    problems = _refused(_zip({"bomb.pdf": b"%PDF-" + b"\x00" * (8 * 1024 * 1024)}))
    assert "zip bomb" in problems[0]


def test_names_that_escape_the_zip_or_hide_their_type_are_refused():
    problems = _refused(
        _zip(
            {
                "../../etc/evil.pdf": _pdf(),
                "C:/Windows/evil.pdf": _pdf(),
                "invoice\u202efdp.exe": b"MZ",
            }
        )
    )
    text = "\n".join(problems)
    assert text.count("points outside the ZIP") == 2
    assert "invisible characters" in text


def test_a_link_and_a_password_protected_entry_are_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        link = zipfile.ZipInfo("passwd.pdf")
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "/etc/passwd")
    assert "passwd.pdf is a link" in _refused(buffer.getvalue())[0]

    # Python never writes an encrypted entry, so set the "encrypted" bit by hand
    # in the local header and the central directory, as an encrypting tool would.
    locked = bytearray(_zip({"locked.pdf": _pdf()}))
    locked[6] |= 0x1
    central = locked.index(b"PK\x01\x02")
    locked[central + 8] |= 0x1
    assert "locked.pdf is password-protected" in _refused(bytes(locked))[0]


def test_an_office_file_a_self_extractor_and_an_empty_or_broken_zip_are_refused():
    docx = _zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w/>"})
    assert "Word, Excel or PowerPoint" in _refused(docx)[0]
    assert "not an ordinary ZIP" in _refused(b"MZ\x90" + _zip({"a.pdf": _pdf()}))[0]
    assert "empty" in _refused(_zip({}))[0]
    assert "no files" in _refused(_zip({"__MACOSX/._x": b"x"}))[0]
    assert "damaged" in _refused(b"PK\x03\x04 not really a zip")[0]


def test_too_many_files_or_too_large_a_zip_is_refused():
    many = _zip({f"doc-{n}.png": _png() for n in range(zip_scan.MAX_FILES + 1)})
    assert f"at most {zip_scan.MAX_FILES}" in _refused(many)[0]
    with pytest.raises(FileTooLargeError, match="at most 5 MB"):
        check_zip(b"PK\x03\x04" + b"\x00" * (5 * 1024 * 1024), max_file_bytes=LIMIT)


def test_the_refusal_names_every_file_to_fix():
    message = str(ZipRejectedError([f"file{n}.exe is a .exe file." for n in range(10)]))
    assert message.startswith("The ZIP was not stored. Nothing in it was kept.")
    assert "file0.exe" in message and "file7.exe" in message
    assert "and 2 more" in message
