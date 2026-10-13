#!/usr/bin/env bash
# Build the Lambda deployment bundle (Python 3.12, arm64) into build/lambda. No Docker needed.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=build/lambda
rm -rf "$OUT" && mkdir -p "$OUT"

[ -f brand.md ] || { echo "brand.md not found - run 'uv run agent init' and fill it in." >&2; exit 1; }

uv export --no-dev --no-hashes --no-emit-project --frozen -q > build/requirements.txt
uv pip install -q --target "$OUT" --python-platform aarch64-manylinux2014 --python-version 3.12 \
  --only-binary=:all: -r build/requirements.txt

# The Google client ships discovery docs for ~500 APIs (~100 MB); only Gmail is used.
find "$OUT/googleapiclient/discovery_cache/documents" -type f ! -name 'gmail.v1.json' -delete
# Not needed at runtime on Lambda.
rm -rf "$OUT"/pygments "$OUT"/bin
find "$OUT" -name '__pycache__' -type d -prune -exec rm -rf {} +

cp -R src/marketing_agent "$OUT/"
cp brand.md "$OUT/"
echo "Lambda bundle: $(du -sh "$OUT" | cut -f1) in $OUT"
