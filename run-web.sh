#!/bin/bash
# Launch pravachan-web with the repo venv active.
# The web app spawns jobs via `python3` from PATH — it MUST be the venv python,
# otherwise pipeline deps (fastapi/weasyprint/...) are missing.
cd "$(dirname "$0")"
source .venv/bin/activate
# WeasyPrint (macOS) needs Homebrew's pango/gobject dylibs on the fallback path —
# without this, `import weasyprint` fails with "cannot load library 'libgobject-2.0-0'".
export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
exec pravachan-web "$@"
