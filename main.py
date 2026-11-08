"""Render entry point: restore the bundled bot source, then serve its health endpoint."""
import gzip
import os
from pathlib import Path

root = Path(__file__).resolve().parent
compressed_source = root / "bot.py.gz"
source_file = root / "bot.py"

if not source_file.exists() or source_file.stat().st_mtime < compressed_source.stat().st_mtime:
    source_file.write_bytes(gzip.decompress(compressed_source.read_bytes()))

from bot import app  # noqa: E402

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        threaded=True,
    )
