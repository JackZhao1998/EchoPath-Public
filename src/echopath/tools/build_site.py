"""Turn the artifact page into one self-contained HTML file.

The artifact platform wraps the page in a document skeleton and serves the
screenshots as separate files. A file you host yourself gets neither, so this
adds the skeleton back and inlines every image as a data URI -- the result is a
single document that works from any static host, or opened straight off disk.

    python -m echopath.tools.build_site path/to/artifact.html
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import re
from pathlib import Path

from echopath.project_paths import project_root

# The skeleton the artifact host normally supplies, kept to the parts the page
# actually relies on: the viewport meta, a zeroed body margin, and responsive
# images.
SKELETON_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="Five GUI workflows taught to EchoPath and replayed, and the rule that explains which ones held up.">
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; }
  img { max-width: 100%; }
  [hidden] { display: none !important; }
</style>
"""


def inline_images(html: str, source_directory: Path) -> tuple[str, int]:
    """Replace every src="shots/x.jpg" with the image itself."""
    count = 0

    def swap(match: re.Match) -> str:
        nonlocal count
        relative = match.group(1)
        path = source_directory / relative
        if not path.is_file():
            raise FileNotFoundError(f"referenced image is missing: {path}")
        media_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        count += 1
        return f'src="data:{media_type};base64,{encoded}"'

    return re.sub(r'src="(shots/[^"]+)"', swap, html), count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="artifact HTML; shots/ is relative to this file")
    parser.add_argument("--output", type=Path, help="output HTML (default: <project>/site/index.html)")
    args = parser.parse_args(argv)
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve() if args.output else project_root() / "site" / "index.html"
    html = source.read_text(encoding="utf-8")

    # The artifact file starts at <title> and has no <head>/<body> of its own.
    title_match = re.search(r"<title>(.*?)</title>", html, re.S)
    title = title_match.group(1).strip() if title_match else "EchoPath"

    body, replaced = inline_images(html, source.parent)

    document = (
        SKELETON_HEAD
        + f"<title>{title}</title>\n</head>\n<body>\n"
        + body.replace(f"<title>{title}</title>\n", "", 1)
        + "\n</body>\n</html>\n"
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")

    size_kb = output.stat().st_size // 1024
    print(f"wrote {output}  ({size_kb} KB, {replaced} images inlined)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
