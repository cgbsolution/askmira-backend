"""Copy the local upload directory into the Supabase Storage bucket.

    cd backend
    .venv/Scripts/python.exe scripts/copy_uploads_to_supabase.py --dry-run   # list, touch nothing
    .venv/Scripts/python.exe scripts/copy_uploads_to_supabase.py             # copy

Run once when switching STORAGE_BACKEND from local to supabase, before the
switched app serves anyone. A file's key is its path under the upload
directory, so each one lands at exactly the name its database row already
holds; no row changes.

Reads SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and SUPABASE_STORAGE_BUCKET from
the environment or .env, and prints the project and bucket (never the key)
before it writes. Creates the bucket, private, if it does not exist, and
refuses to write into a public one.

Every copy is read back and compared before it counts. Idempotent: a re-run
uploads again over the same keys. Deletes nothing, locally or in the bucket -
remove the local directory yourself once the app is serving from Supabase.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.services.storage import StorageError, SupabaseStorage
from app.services.storage.base import check_key
from app.services.storage.local import DEFAULT_ROOT

#: Parallel uploads. Enough to be quick, few enough not to trip rate limits.
CONCURRENCY = 8


def _stored_files(root: Path) -> tuple[list[tuple[str, Path]], list[Path]]:
    """(key, path) for every stored file, and the paths that are not keys."""
    files: list[tuple[str, Path]] = []
    skipped: list[Path] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        # A leading dot is an interrupted write's temporary file.
        if path.name.startswith("."):
            skipped.append(path)
            continue
        try:
            files.append((check_key(path.relative_to(root).as_posix()), path))
        except StorageError:
            skipped.append(path)
    return files, skipped


async def _copy(storage: SupabaseStorage, key: str, path: Path, gate: asyncio.Semaphore) -> str:
    """Why the copy failed, or an empty string when it landed and reads back intact."""
    data = path.read_bytes()
    async with gate:
        try:
            await storage.put(key, data)
            if await storage.get(key) != data:
                return "the copy reads back differently"
        except StorageError as exc:
            return str(exc)
    return ""


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(settings.FILE_STORAGE_ROOT or DEFAULT_ROOT),
        help="the local upload directory (default: FILE_STORAGE_ROOT, else backend/uploads)",
    )
    parser.add_argument("--dry-run", action="store_true", help="list what would be copied")
    args = parser.parse_args()

    root = args.source.resolve()
    if not root.is_dir():
        print(f"No upload directory at {root}; nothing to copy.")
        return 0

    files, skipped = _stored_files(root)
    storage = SupabaseStorage(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_ROLE_KEY,
        settings.SUPABASE_STORAGE_BUCKET,
    )
    size = sum(path.stat().st_size for _, path in files)
    print(f"From: {root}")
    print(f"To:   {storage.describe()}")
    print(f"{len(files)} files, {size / 1024 / 1024:.1f} MB")
    for path in skipped:
        print(f"  skipped, not a storage key: {path.relative_to(root).as_posix()}")

    if args.dry_run:
        for key, path in files:
            print(f"  would copy {key} ({path.stat().st_size} bytes)")
        return 0

    try:
        print(f"Bucket '{storage.bucket}': {await storage.ensure_private_bucket()}")
    except StorageError as exc:
        print(f"Stopped before copying anything: {exc}")
        return 1

    gate = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(_copy(storage, key, path, gate) for key, path in files))
    failed = [(key, reason) for (key, _), reason in zip(files, results, strict=True) if reason]
    for key, reason in failed:
        print(f"  FAILED {key}: {reason}")
    print(f"Copied and verified {len(files) - len(failed)} of {len(files)}.")
    if failed:
        print("Re-run to retry; files already copied are simply written again.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
