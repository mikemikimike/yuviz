#!/usr/bin/env python3
"""Backfill kb_documents.byte_size for live documents uploaded before the column existed (safe to re-run).

Until it runs those documents count as 0 bytes toward the per-tenant storage quota. The size is the length of the
stored file (the document's source_ref), so run it where the knowledge service's storage is reachable, with the
same KNOWLEDGE_STORAGE_ROOT. A document whose file can't be read keeps a NULL size and is listed.

Requires POSTGRES_DSN.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from services.knowledge.storage import LocalStorageProvider, StorageProvider  # noqa: E402


async def backfill(
    conn: Any, storage: StorageProvider, *, tenant_id: Any | None = None, dry_run: bool = False,
) -> tuple[int, list[str]]:
    """Returns (documents sized, ids whose file could not be read). Touches only rows still NULL, so re-runs and a
    concurrent upload (which writes its own size) are safe."""
    rows = await conn.fetch(
        "SELECT id, source_ref FROM kb_documents "
        "WHERE byte_size IS NULL AND deleted_at IS NULL AND source_ref <> '' "
        "AND ($1::uuid IS NULL OR tenant_id = $1) ORDER BY created_at",
        tenant_id,
    )
    sized, unreadable = 0, []
    for row in rows:
        try:
            size = len(await storage.read(row["source_ref"]))
        except OSError:
            unreadable.append(str(row["id"]))
            continue
        if not dry_run:
            await conn.execute(
                "UPDATE kb_documents SET byte_size = $2 WHERE id = $1 AND byte_size IS NULL", row["id"], size,
            )
        sized += 1
    return sized, unreadable


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", default=None, help="restrict to one tenant id")
    parser.add_argument("--dry-run", action="store_true", help="read the sizes but do not write them")
    args = parser.parse_args()

    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("POSTGRES_DSN is not set", file=sys.stderr)
        return 1

    # Cross-tenant by design: a hand-run maintenance script.
    conn = await asyncpg.connect(dsn)
    try:
        sized, unreadable = await backfill(conn, LocalStorageProvider(), tenant_id=args.tenant_id, dry_run=args.dry_run)
    finally:
        await conn.close()
    print(f"sized {sized} document(s){' (dry run)' if args.dry_run else ''}; {len(unreadable)} unreadable")
    for doc_id in unreadable:
        print(f"  unreadable: {doc_id}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
