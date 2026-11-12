"""Checking a ZIP of an employee's documents before it is stored.

HR may attach one employee's papers as a single ZIP (HR only, at most 5 MB).
Nothing in it is trusted. Every file inside is opened and checked, and if any
one of them fails, the whole ZIP is refused with the reason for each file, so
nothing unchecked reaches storage. What is stored is a new ZIP written from the
checked files, never the uploaded bytes: whatever the checks did not read (an
archive comment, extra fields, data hidden before or after the archive) is
left behind.

**The archive**

- starts like a ZIP, not a program (a self-extracting .exe is refused);
- is not a Word, Excel or PowerPoint file (those are ZIPs too, and can carry
  macros);
- holds 1 to 50 files, none password-protected (it could not be checked), none
  a link, none with a name that points outside the archive (``../``, ``C:\\``)
  or is dressed up with invisible characters to hide its real type;
- uses an ordinary compression method.

**Every file**, read with a cap whatever size the ZIP claims, so a "zip bomb"
stops at the cap instead of filling the server's memory:

- unpacks to no more than the workspace's file limit and no more than 100
  times its packed size; all files together to no more than 50 MB;
- is named .pdf, .png, .jpg or .jpeg and is one, judged from its own bytes: a
  program renamed .pdf, a ZIP inside the ZIP, a web page, are all refused;
- an **image** must decode completely (a malformed image built to hit a bug in
  a viewer does not), within a pixel budget, and is then saved again from its
  pixels, which drops whatever was hidden in it: metadata, data after the
  image;
- a **PDF** must open, must not be locked with a password, may not carry data
  after its end, and no object anywhere in it, reachable or not, may hold
  JavaScript, launch a program, open another file, import data, embed a file or
  media, or hold an XFA form (which can carry scripts). It is then compressed
  like every upload.

There is no virus-signature scan here: that needs an antivirus service (such as
ClamAV) beside the API. These checks refuse anything that is not a plain
document, which is what such a scan would mostly be looking for in a ZIP of ID
proofs and certificates.

Pure and blocking (CPU work, no I/O): callers run it in a worker thread.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from PIL import Image, ImageOps
from pypdf import PasswordType, PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject

from app.services.storage.compress import DOCUMENT, compress
from app.services.storage.sniff import FileTooLargeError, size_label, sniff

__all__ = [
    "MAX_FILES",
    "MAX_UNPACKED_BYTES",
    "MAX_ZIP_BYTES",
    "ZIP_MIME",
    "CheckedZip",
    "CleanFile",
    "FileProblemError",
    "ZipRejectedError",
    "check_document",
    "check_zip",
    "looks_like_zip",
]

#: The largest ZIP of one employee's documents (or the workspace's own upload
#: limit, when that is lower).
MAX_ZIP_BYTES = 5 * 1024 * 1024
#: Files in one ZIP: a full set of joining papers is ten to twenty.
MAX_FILES = 50
#: All files together, unpacked.
MAX_UNPACKED_BYTES = 50 * 1024 * 1024
#: A file may unpack to at most this many times its packed size. Scans and
#: photos barely compress (2-3x); a "zip bomb" compresses a thousand-fold.
MAX_RATIO = 100
#: An image with more pixels than this is not decoded (80 MP: far beyond a scan).
MAX_PIXELS = 80_000_000
#: A PDF with more objects than this is not walked; a scanned ID has a few dozen.
_MAX_PDF_OBJECTS = 200_000

ZIP_MIME = "application/zip"

_LOCAL_HEADER = b"PK\x03\x04"
_EMPTY_ARCHIVE = b"PK\x05\x06"
_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}
#: Files an operating system puts into a ZIP that nobody meant to upload.
_NOISE = re.compile(r"(^|/)(__MACOSX/|\._|\.DS_Store$|Thumbs\.db$|desktop\.ini$)", re.I)
_TYPE_OF_EXTENSION = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
_EXTENSION_OF_TYPE = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}
_IMAGE_FORMAT = {"image/png": "PNG", "image/jpeg": "JPEG"}
#: Control characters, and the invisible direction marks that make
#: "invoice<RLO>fdp.exe" read as "invoiceexe.pdf".
_HIDDEN_CHARS = re.compile("[\x00-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069]")
#: Characters Windows will not put in a file name.
_UNSAFE_CHARS = re.compile(r'[<>:"|?*]')

#: A dictionary key that by itself means the PDF can do something, not just show.
_DANGEROUS_KEYS = {
    "/JS": "JavaScript",
    "/JavaScript": "JavaScript",
    "/Launch": "an action that starts a program",
    "/EmbeddedFile": "a file hidden inside it",
    "/EmbeddedFiles": "a file hidden inside it",
    "/RichMedia": "embedded media",
    "/XFA": "an XFA form, which can carry scripts",
}
#: Action types (`/S`) that run or open something.
_DANGEROUS_ACTIONS = {
    "/JavaScript": "JavaScript",
    "/Launch": "an action that starts a program",
    "/GoToR": "a link that opens another file",
    "/GoToE": "a link into a file hidden inside it",
    "/ImportData": "an action that imports data from another file",
    "/RichMediaExecute": "embedded media",
}
#: The same words in the raw bytes, for objects a parser might not reach.
_RAW_DANGER = re.compile(rb"/(JavaScript|Launch|EmbeddedFiles?|RichMedia|XFA)(?=[\s/<>\[\]()%])")


class FileProblemError(ValueError):
    """One file is not a clean document. The message finishes "<file name> …"."""


class _OverBudgetError(FileProblemError):
    """All files together unpack past the limit: the rest are not read."""


class ZipRejectedError(ValueError):
    """The ZIP was refused. `problems` has one line per file (or one for the archive)."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(describe(problems))


def describe(problems: list[str]) -> str:
    """The refusal as one message: what to fix, one line per file, the first eight."""
    if len(problems) == 1:
        return f"The ZIP was not stored. {problems[0]}"
    lines = [f"• {problem}" for problem in problems[:8]]
    if len(problems) > 8:
        lines.append(f"• and {len(problems) - 8} more.")
    return "The ZIP was not stored. Nothing in it was kept. Fix these and upload it again:\n" + (
        "\n".join(lines)
    )


@dataclass(frozen=True, slots=True)
class CleanFile:
    """A file that passed every check, ready to store."""

    #: Its path inside the archive: safe, and ending in its real type.
    name: str
    data: bytes
    mime_type: str


@dataclass(frozen=True, slots=True)
class CheckedZip:
    """A ZIP rebuilt from checked files only."""

    data: bytes
    files: tuple[str, ...]
    uploaded_size: int


def looks_like_zip(data: bytes) -> bool:
    """Whether the bytes are a ZIP (or claim to be): the upload path to take."""
    return data[:2] == b"PK"


def check_zip(
    data: bytes, *, max_file_bytes: int, max_zip_bytes: int = MAX_ZIP_BYTES
) -> CheckedZip:
    """Check every file in a ZIP and rebuild it from them, or refuse it whole.

    Raises `FileTooLargeError` for a ZIP over `max_zip_bytes`, and
    `ZipRejectedError` naming every problem otherwise.
    """
    if len(data) > max_zip_bytes:
        raise FileTooLargeError(
            f"The ZIP is {size_label(len(data), up=True)}. "
            f"A ZIP of documents can be at most {size_label(max_zip_bytes)}."
        )
    if data.startswith(_EMPTY_ARCHIVE):
        raise ZipRejectedError(["The ZIP is empty."])
    if not data.startswith(_LOCAL_HEADER):
        raise ZipRejectedError(
            [
                "This is not an ordinary ZIP: it may be split into parts, or a program "
                "that unpacks itself. Make a plain ZIP of the documents."
            ]
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        infos = archive.infolist()
    except Exception as exc:  # any failure to read the directory is a damaged ZIP
        raise ZipRejectedError(["The ZIP is damaged and cannot be opened."]) from exc

    if any(info.filename.replace("\\", "/").lstrip("/") == "[Content_Types].xml" for info in infos):
        raise ZipRejectedError(
            [
                "This is a Word, Excel or PowerPoint file, not a ZIP of documents. "
                "Save it as a PDF and upload that."
            ]
        )
    entries = [info for info in infos if not info.is_dir() and not _NOISE.search(info.filename)]
    if not entries:
        raise ZipRejectedError(["The ZIP has no files in it."])
    if len(entries) > MAX_FILES:
        raise ZipRejectedError(
            [f"The ZIP holds {len(entries)} files. Put at most {MAX_FILES} in one ZIP."]
        )

    problems: list[str] = []
    kept: list[tuple[CleanFile, tuple[int, int, int, int, int, int]]] = []
    used: set[str] = set()
    budget = MAX_UNPACKED_BYTES
    for info in entries:
        try:
            path = _safe_path(info)
            content = _read(archive, info, max_file_bytes=max_file_bytes, budget=budget)
            budget -= len(content)
            clean = check_document(path, content)
        except FileProblemError as problem:
            problems.append(f"{_shown(info.filename)} {problem}")
            if isinstance(problem, _OverBudgetError):
                break
            continue
        clean = replace(clean, name=_unique(clean.name, used))
        kept.append((clean, info.date_time))
    if problems:
        raise ZipRejectedError(problems)
    return CheckedZip(
        data=_rebuild(kept), files=tuple(clean.name for clean, _ in kept), uploaded_size=len(data)
    )


def check_document(name: str, data: bytes) -> CleanFile:
    """One file from a ZIP: a PDF, PNG or JPEG, judged from its bytes, made clean.

    Raises `FileProblemError` saying what is wrong.
    """
    extension = PurePosixPath(name).suffix.lower()
    if extension not in _TYPE_OF_EXTENSION:
        kind = f"a {extension} file" if extension else "a file with no type in its name"
        raise FileProblemError(f"is {kind}. Only PDF, PNG and JPEG files can be in the ZIP.")
    if not data:
        raise FileProblemError("is empty.")
    kind = sniff(data[:16])
    if kind is None:
        raise FileProblemError(_what_it_really_is(data))
    if kind.mime_type == "application/pdf":
        cleaned = _clean_pdf(data)
    else:
        cleaned = _clean_image(data, kind.mime_type)
    if _TYPE_OF_EXTENSION[extension] != kind.mime_type:
        # A PNG named .jpg is harmless; it is stored under the name of what it is.
        name = name[: -len(extension)] + _EXTENSION_OF_TYPE[kind.mime_type]
    return CleanFile(name=name, data=cleaned, mime_type=kind.mime_type)


# ── The archive ─────────────────────────────────────────────────────────────


def _shown(filename: str) -> str:
    """A file's name as a message shows it: no hidden characters, not too long."""
    visible = _HIDDEN_CHARS.sub("?", filename.replace("\\", "/"))
    return visible if len(visible) <= 120 else "…" + visible[-119:]


def _safe_path(info: zipfile.ZipInfo) -> str:
    """The entry's path, checked and made safe to unpack anywhere."""
    if (info.external_attr >> 16) & 0o170000 == 0o120000:
        raise FileProblemError("is a link to somewhere else, not a file.")
    if info.flag_bits & 0x1:
        raise FileProblemError(
            "is password-protected, so it cannot be checked. Add it without a password."
        )
    if info.compress_type not in _METHODS:
        raise FileProblemError(
            "is compressed in a way AskMiraa does not open. Make the ZIP again with your "
            "computer's own 'Compress' or 'Send to → Compressed (zipped) folder'."
        )
    raw = info.filename.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise FileProblemError("has a name that points outside the ZIP.")
    if _HIDDEN_CHARS.search(raw):
        raise FileProblemError(
            "has invisible characters in its name, which can hide its real type."
        )
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise FileProblemError("has a name that points outside the ZIP.")
    if len(parts) > 8:
        raise FileProblemError("is in too many folders inside the ZIP.")
    safe = [(_UNSAFE_CHARS.sub("_", part).strip(" .") or "file")[:100] for part in parts]
    return "/".join(safe)


def _read(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, max_file_bytes: int, budget: int
) -> bytes:
    """An entry's bytes, read no further than every limit, whatever size it declares."""
    if info.file_size > max_file_bytes:
        raise FileProblemError(
            f"is larger than the {size_label(max_file_bytes)} limit for one file."
        )
    ratio_cap = max(info.compress_size, 1) * MAX_RATIO
    if info.file_size > ratio_cap:
        raise FileProblemError(_BOMB)
    cap = min(max_file_bytes, ratio_cap, max(budget, 0))
    try:
        with archive.open(info) as handle:
            # Reading to the end checks the file's checksum; a damaged file raises.
            content = handle.read(cap + 1)
    except Exception as exc:  # any failure to unpack is a damaged entry
        raise FileProblemError("is damaged and cannot be unpacked.") from exc
    if len(content) > cap:
        if cap == budget:
            raise _OverBudgetError(
                f"takes the ZIP past {size_label(MAX_UNPACKED_BYTES)} unpacked, all files together."
            )
        if cap == ratio_cap:
            raise FileProblemError(_BOMB)
        raise FileProblemError(
            f"is larger than the {size_label(max_file_bytes)} limit for one file."
        )
    return content


_BOMB = (
    "unpacks to far more than its packed size, the way a 'zip bomb' is built to "
    "fill a server. It was not opened."
)


def _unique(name: str, used: set[str]) -> str:
    """The name, with " (2)", " (3)"… added when another file already has it."""
    candidate, number = name, 1
    path = PurePosixPath(name)
    while candidate.lower() in used:
        number += 1
        candidate = str(path.with_name(f"{path.stem} ({number}){path.suffix}"))
    used.add(candidate.lower())
    return candidate


def _rebuild(files: list[tuple[CleanFile, tuple[int, int, int, int, int, int]]]) -> bytes:
    """A new ZIP holding exactly the checked files, and nothing else."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for clean, date_time in files:
            stamp = date_time if date_time[0] >= 1980 else (1980, 1, 1, 0, 0, 0)
            entry = zipfile.ZipInfo(clean.name, date_time=stamp)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, clean.data)
    return out.getvalue()


# ── One file ────────────────────────────────────────────────────────────────


def _what_it_really_is(data: bytes) -> str:
    head = data[:8]
    if (
        head.startswith(b"MZ")
        or head.startswith(b"\x7fELF")
        or head[:4]
        in (
            b"\xca\xfe\xba\xbe",
            b"\xcf\xfa\xed\xfe",
            b"\xce\xfa\xed\xfe",
        )
    ):
        return "is a program, not a document."
    if head.startswith(b"PK"):
        return "is a ZIP (or an Office file) inside the ZIP. Put the documents in directly."
    if head.startswith(b"#!"):
        return "is a script, not a document."
    if data.lstrip()[:1] == b"<":
        return "is a web page or script, not a document."
    return "is named like a document, but its contents are not a PDF, PNG or JPEG."


def _clean_image(data: bytes, mime_type: str) -> bytes:
    """Decode the whole image, then save it again from its pixels alone."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format != _IMAGE_FORMAT[mime_type]:
                raise FileProblemError("is not the kind of image it starts like.")
            width, height = probe.size
            if width * height > MAX_PIXELS:
                raise FileProblemError(
                    f"is a {round(width * height / 1_000_000)}-megapixel image, too large to check."
                )
            probe.verify()
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened) or opened
            if max(image.size) > DOCUMENT.max_side:
                image.thumbnail((DOCUMENT.max_side, DOCUMENT.max_side), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            if mime_type == "image/jpeg":
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                image.save(out, "JPEG", quality=DOCUMENT.quality, optimize=True)
            else:
                image.save(out, "PNG", optimize=True)
            return out.getvalue()
    except FileProblemError:
        raise
    except Exception as exc:  # a decoder error of any kind is a malformed image
        raise FileProblemError("is a damaged or malformed image.") from exc


def _clean_pdf(data: bytes) -> bytes:
    """Refuse a PDF that can do anything but show pages; compress the rest."""
    end = data.rfind(b"%%EOF")
    if end == -1:
        raise FileProblemError("is not a complete PDF.")
    if data[end + 5 :].strip(b" \t\r\n\x00\x0c"):
        raise FileProblemError("has data hidden after the end of the PDF.")
    raw = _RAW_DANGER.search(data)
    if raw is not None:
        raise FileProblemError(_active(_DANGEROUS_KEYS["/" + raw.group(1).decode()]))
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
            raise FileProblemError(
                "is locked with a password, so it cannot be checked. Add it without the password."
            )
        found = _active_content(reader)
    except FileProblemError:
        raise
    except Exception as exc:  # pypdf raises many types for a broken file
        raise FileProblemError("is a damaged PDF that cannot be opened.") from exc
    if found is not None:
        raise FileProblemError(_active(found))
    return compress(data, "application/pdf", DOCUMENT)


def _active(what: str) -> str:
    return f"contains {what}, which can run on the computer that opens it. Refused."


def _active_content(reader: PdfReader) -> str | None:
    """What the PDF could do beyond showing pages, from every object it holds."""
    seen = 0

    def visit(obj: object, depth: int) -> str | None:
        nonlocal seen
        seen += 1
        if seen > _MAX_PDF_OBJECTS:
            raise FileProblemError("is too complex a PDF to check.")
        if depth > 64 or isinstance(obj, IndirectObject):
            # Indirect objects are each visited from the cross-reference table.
            return None
        if isinstance(obj, DictionaryObject):  # streams are dictionaries too
            for key, value in obj.items():
                reason = _DANGEROUS_KEYS.get(str(key))
                if reason is not None:
                    return reason
                if key == "/S":
                    action = value.get_object() if isinstance(value, IndirectObject) else value
                    if isinstance(action, NameObject) and str(action) in _DANGEROUS_ACTIONS:
                        return _DANGEROUS_ACTIONS[str(action)]
                found = visit(value, depth + 1)
                if found is not None:
                    return found
        elif isinstance(obj, ArrayObject):
            for item in obj:
                found = visit(item, depth + 1)
                if found is not None:
                    return found
        return None

    numbers: set[tuple[int, int]] = set()
    for generation, table in reader.xref.items():
        numbers.update((number, generation) for number in table)
    numbers.update((number, 0) for number in reader.xref_objStm)
    for number, generation in sorted(numbers):
        try:
            obj = reader.get_object(IndirectObject(number, generation, reader))
        except Exception:  # an object nothing can read cannot run either
            continue
        found = visit(obj, 0)
        if found is not None:
            return found
    return visit(reader.trailer, 0)
