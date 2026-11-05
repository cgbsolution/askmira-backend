"""Making an uploaded file smaller before it is stored.

Every file a person uploads (an ID proof photographed on a phone, a scanned
certificate, a logo, a receipt) passes through `compress` before it is written.
The aim is storage space, without changing what the file is:

* **Photos (JPEG, WebP)** are turned upright, scaled down so the longer side is
  at most the profile's size, and saved again at a quality where text on a
  document stays sharp. Their hidden metadata goes with it, including the GPS
  position a phone camera writes into every photo.
* **PNG images** keep their transparency and stay PNG. They are scaled down the
  same way and saved with the strongest lossless compression.
* **PDFs** keep every page and its text. Large scanned images inside are scaled
  and saved again as JPEG, content left uncompressed is compressed, and repeated
  objects are stored once.

What is never changed:

* A **digitally signed PDF** (a TRACES Form 16, an e-signed letter). Rewriting
  it breaks the signature, and the signature is the point of the document.
* A **password-protected PDF**, which cannot be read without the password.
* A file that something went wrong with: anything unexpected keeps the original.
* A file the work would not make meaningfully smaller: the original is kept.

The result is always a file of the same type, so its extension, its stored type
and every viewer that opens it stay right.

Pure and blocking (CPU work, no I/O): callers run it in a worker thread.
"""

from __future__ import annotations

import io
import logging
import zlib
from dataclasses import dataclass

__all__ = ["DOCUMENT", "LOGO", "PHOTO", "Profile", "compress"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Profile:
    """How far a kind of upload is shrunk."""

    #: The longer side of an image after scaling, in pixels.
    max_side: int
    #: JPEG / WebP quality (1-95).
    quality: int


#: Documents and attachments: an A4 page at about 200 dpi stays readable.
DOCUMENT = Profile(max_side=2400, quality=82)
#: A face: a profile photo or a punch selfie.
PHOTO = Profile(max_side=1024, quality=82)
#: Logos, letterhead art, a scanned signature: printed, so kept crisp.
LOGO = Profile(max_side=1600, quality=90)

#: A smaller result is kept only when it saves at least this share of the size.
_MIN_SAVING = 0.05
#: A re-encoded JPEG that was not scaled must save more: every lossy save costs a
#: little detail, and that is not worth paying for a few per cent.
_MIN_SAVING_REENCODE = 0.10
#: Files this small are not worth the work.
_SMALL_FILE = 32 * 1024
#: Images larger than this (in pixels) are left alone rather than decoded:
#: decoding one would take more memory than an upload should.
_MAX_PIXELS = 80_000_000
#: Across one PDF, stop shrinking images after this many decoded pixels.
_PDF_PIXEL_BUDGET = 600_000_000
#: An image inside a PDF smaller than this (encoded) is left as it is.
_PDF_IMAGE_MIN_BYTES = 48 * 1024
#: Filters pypdf decodes without loss, alone or chained.
_LOSSLESS_FILTERS = {
    "/FlateDecode",
    "/ASCII85Decode",
    "/ASCIIHexDecode",
    "/LZWDecode",
    "/RunLengthDecode",
}

_JPEG = "image/jpeg"
_PNG = "image/png"
_WEBP = "image/webp"
_PDF = "application/pdf"


def compress(data: bytes, mime_type: str, profile: Profile = DOCUMENT) -> bytes:
    """The smaller version of a file, or the same bytes when there is none.

    `mime_type` is the type read from the file's own bytes (`sniff`), never
    the type the uploader's browser claimed.
    """
    if len(data) < _SMALL_FILE:
        return data
    try:
        if mime_type in (_JPEG, _PNG, _WEBP):
            smaller = _image(data, mime_type, profile)
        elif mime_type == _PDF:
            smaller = _pdf(data, profile)
        else:
            return data
    except Exception:  # any failure keeps the original, by design
        logger.info(
            "Kept a %s upload as it was: it could not be compressed", mime_type, exc_info=True
        )
        return data
    if smaller is None or len(smaller) >= len(data):
        return data
    return smaller


def _worth_it(before: int, after: int, minimum: float = _MIN_SAVING) -> bool:
    return after <= before * (1 - minimum)


# ═══════════════════════════════════════════════════════════════════════════
# Images
# ═══════════════════════════════════════════════════════════════════════════


def _image(data: bytes, mime_type: str, profile: Profile) -> bytes | None:
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as opened:
        if opened.width * opened.height > _MAX_PIXELS:
            return None
        if getattr(opened, "is_animated", False):
            return None
        if opened.mode in ("CMYK", "I", "I;16", "I;16B", "I;16L", "F"):
            # CMYK JPEGs (often inverted Adobe ones) and 16-bit or float images
            # do not survive a round trip faithfully. They are rare; keep them.
            return None
        icc = opened.info.get("icc_profile")
        if opened.format == "JPEG":
            # Let the decoder scale by 1/2, 1/4 or 1/8 while decoding: far less
            # memory and time for a 48-megapixel phone photo.
            opened.draft(opened.mode, (profile.max_side, profile.max_side))
        image = ImageOps.exif_transpose(opened) or opened
        image.load()

    scaled = max(image.size) > profile.max_side
    if scaled:
        if image.mode == "P":
            image = image.convert("RGBA")
        image.thumbnail((profile.max_side, profile.max_side), Image.Resampling.LANCZOS)

    out = io.BytesIO()
    extra = {"icc_profile": icc} if icc else {}
    if mime_type == _JPEG:
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        # Baseline, not progressive: logos and photos are also drawn into
        # generated PDFs (letters, payslips), and every PDF library reads baseline.
        image.save(out, "JPEG", quality=profile.quality, optimize=True, **extra)
    elif mime_type == _WEBP:
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "A" in image.mode else "RGB")
        image.save(out, "WEBP", quality=profile.quality, method=6, **extra)
    else:
        image.save(out, "PNG", optimize=True, **extra)
    result = out.getvalue()

    lossy = mime_type in (_JPEG, _WEBP)
    minimum = _MIN_SAVING_REENCODE if lossy and not scaled else _MIN_SAVING
    return result if _worth_it(len(data), len(result), minimum) else None


# ═══════════════════════════════════════════════════════════════════════════
# PDFs
# ═══════════════════════════════════════════════════════════════════════════


def _is_signed(data: bytes) -> bool:
    """A digital signature's dictionary holds a byte range over the file.

    The signature's `/Contents` must sit uncompressed in the file for that range
    to be checkable, so its dictionary cannot hide inside a compressed object
    stream: searching the raw bytes finds every signature.
    """
    return b"/ByteRange" in data


def _pdf(data: bytes, profile: Profile) -> bytes | None:
    from pypdf import PdfReader, PdfWriter

    if _is_signed(data):
        return None
    # Strict: a file the reader would have to repair is kept as it was, rather
    # than rewritten from a repaired reading that might have lost something.
    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted:
        return None
    pages = len(reader.pages)
    root = reader.trailer["/Root"].get_object()
    acroform = root.get("/AcroForm")
    if acroform is not None and acroform.get_object().get("/SigFlags"):
        return None

    writer = PdfWriter(clone_from=reader)
    budget = [_PDF_PIXEL_BUDGET]
    seen: set[int] = set()
    for page in writer.pages:
        _shrink_images(page.get("/Resources"), profile, budget, seen)
    _compress_plain_streams(writer)
    writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)

    out = io.BytesIO()
    writer.write(out)
    result = out.getvalue()
    if not _worth_it(len(data), len(result)):
        return None

    # The rewritten file must open and hold every page the original held.
    check = PdfReader(io.BytesIO(result), strict=True)
    if len(check.pages) != pages:
        return None
    return result


def _shrink_images(resources, profile: Profile, budget: list[int], seen: set[int]) -> None:
    """Shrink the images a page (or a form inside it) draws, each once."""
    from pypdf.generic import IndirectObject

    if resources is None:
        return
    resources = resources.get_object()
    xobjects = resources.get("/XObject")
    if xobjects is None:
        return
    for reference in xobjects.get_object().values():
        key = reference.idnum if isinstance(reference, IndirectObject) else id(reference)
        if key in seen:
            continue
        seen.add(key)
        stream = reference.get_object()
        subtype = stream.get("/Subtype")
        if subtype == "/Image":
            _shrink_image(stream, profile, budget)
        elif subtype == "/Form":
            _shrink_images(stream.get("/Resources"), profile, budget, seen)


def _filters(stream) -> list[str]:
    value = stream.get("/Filter")
    if value is None:
        return []
    value = value.get_object()
    if isinstance(value, list):
        return [str(item.get_object()) for item in value]
    return [str(value)]


def _components(stream) -> int | None:
    """1 for grey, 3 for RGB; None for anything this does not re-encode."""
    space = stream.get("/ColorSpace")
    if space is None:
        return None
    space = space.get_object()
    if space == "/DeviceGray":
        return 1
    if space == "/DeviceRGB":
        return 3
    if isinstance(space, list) and len(space) == 2 and space[0] == "/ICCBased":
        components = space[1].get_object().get("/N")
        if components in (1, 3):
            return int(components)
    return None


def _shrink_image(stream, profile: Profile, budget: list[int]) -> None:
    """Scale and re-save one image XObject as JPEG, when that is safe and smaller."""
    from PIL import Image
    from pypdf.generic import NameObject, NumberObject

    if stream.get("/ImageMask") or "/Mask" in stream or "/Decode" in stream:
        return
    if stream.get("/BitsPerComponent") != 8:
        return
    components = _components(stream)
    if components is None:
        return
    filters = _filters(stream)
    # A JPEG, possibly wrapped in a text encoding first (ReportLab writes
    # ASCII85 around it); otherwise pixels behind lossless filters, which pypdf
    # decodes itself.
    is_jpeg = bool(filters) and filters[-1] == "/DCTDecode"
    wrapping = filters[:-1] if is_jpeg else filters
    if not filters or not set(wrapping) <= _LOSSLESS_FILTERS:
        return
    raw = stream._data  # the bytes as stored, still encoded
    if len(raw) < _PDF_IMAGE_MIN_BYTES:
        return
    width, height = int(stream.get("/Width", 0)), int(stream.get("/Height", 0))
    if width <= 0 or height <= 0 or width * height > _MAX_PIXELS:
        return
    if budget[0] < width * height:
        return
    budget[0] -= width * height

    mode = "L" if components == 1 else "RGB"
    jpeg = b""
    if is_jpeg:
        # pypdf passes JPEG bytes through undecoded, after any wrapping filter.
        jpeg = stream.get_data() if wrapping else raw
        image = Image.open(io.BytesIO(jpeg))
        if image.mode != mode or image.size != (width, height):
            return
        image.draft(mode, (profile.max_side, profile.max_side))
        image.load()
    else:
        pixels = stream.get_data()
        if len(pixels) != width * height * components:
            return
        image = Image.frombytes(mode, (width, height), pixels)

    scaled = max(image.size) > profile.max_side
    if scaled:
        image.thumbnail((profile.max_side, profile.max_side), Image.Resampling.LANCZOS)

    out = io.BytesIO()
    image.save(out, "JPEG", quality=profile.quality, optimize=True)
    encoded = out.getvalue()
    # Already a JPEG at a size worth keeping: saving it again costs detail, so
    # it has to buy more.
    baseline = len(jpeg) if is_jpeg else len(raw)
    minimum = _MIN_SAVING_REENCODE if is_jpeg and not scaled else _MIN_SAVING
    if not _worth_it(baseline, len(encoded), minimum):
        if not (is_jpeg and wrapping and _worth_it(len(raw), len(jpeg))):
            return
        # Not worth saving again, but the text wrapping around it is pure
        # overhead: store the same JPEG bare, losing nothing.
        encoded, size = jpeg, (width, height)
    else:
        size = image.size

    stream._data = encoded
    if hasattr(stream, "decoded_self"):
        stream.decoded_self = None
    stream[NameObject("/Filter")] = NameObject("/DCTDecode")
    stream.pop("/DecodeParms", None)
    stream[NameObject("/Width")] = NumberObject(size[0])
    stream[NameObject("/Height")] = NumberObject(size[1])


def _compress_plain_streams(writer) -> None:
    """Flate-compress every stream stored with no compression at all.

    Metadata (XMP) streams stay readable, as the PDF standard asks.
    """
    from pypdf.generic import NameObject, StreamObject

    for obj in writer._objects:
        if not isinstance(obj, StreamObject):
            continue
        if "/Filter" in obj or "/DecodeParms" in obj or obj.get("/Type") == "/Metadata":
            continue
        raw = obj._data
        if len(raw) < 256:
            continue
        packed = zlib.compress(raw, 9)
        if len(packed) >= len(raw):
            continue
        obj._data = packed
        if hasattr(obj, "decoded_self"):
            obj.decoded_self = None
        obj[NameObject("/Filter")] = NameObject("/FlateDecode")
