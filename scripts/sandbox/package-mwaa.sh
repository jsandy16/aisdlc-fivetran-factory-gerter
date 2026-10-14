#!/usr/bin/env bash
# plugins.zip for MWAA: the factory_airflow wheel, installed by requirements.txt from /usr/local/airflow/plugins/.
# Usage: scripts/sandbox/package-mwaa.sh   -> build/mwaa/plugins.zip (pass its path as var.mwaa_plugins_zip)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/build/mwaa"
rm -rf "$OUT" && mkdir -p "$OUT/wheel"
(cd "$ROOT/packages" && uv build --package factory-airflow --wheel -q -o "$OUT/wheel")
python3 -c "import sys, zipfile, pathlib
z = zipfile.ZipFile(sys.argv[1], 'w', zipfile.ZIP_DEFLATED)
for w in sorted(pathlib.Path(sys.argv[2]).glob('*.whl')):
    z.write(w, w.name)
z.close()" "$OUT/plugins.zip" "$OUT/wheel"
echo "$OUT/plugins.zip"
