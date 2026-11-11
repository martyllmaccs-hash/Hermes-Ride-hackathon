#!/bin/sh
# Prepare browser wake assets in static/vendor/ (gitignored). Official source
# runtime is compiled locally; official model downloads are checksum-pinned.
# Failure before publication leaves static/vendor/ untouched.
# Requires activated official emsdk 4.0.23, cmake, make, curl, sha256sum, python3.
# 1. Runtime: tools/build_wake_runtime.sh (Apache-2.0).
# 2. Model sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20 (release kws-models; Apache-2.0 per the original
#    ModelScope card pkufool/icefall-kws-zipformer-zh-en-3M-2025-12-20): int8 encoder/joiner, fp32 decoder, tokens.
# 3. kws-words-<wake word>.txt (one per ride_logic.WAKE_WORDS entry) generated from the model's own lexicon (tools/make_keywords.py).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
OUT="$ROOT/static/vendor"
MODEL_URL=https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2
MODEL_SHA=68447f4fbc67e70eee3a93961f36e81e98f47aef73ce7e7ca00885c6cd3616a6

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/dl" "$TMP/out"

get() {  # url sha256 dest
  curl -fsSL --retry 2 --max-time 240 -o "$3" "$1"
  got=$(sha256sum "$3" | cut -d' ' -f1)
  if [ "$got" != "$2" ]; then echo "sha256 mismatch for $1: $got" >&2; exit 1; fi
  echo "ok  $2  $(basename "$3")"
}

sh "$HERE/build_wake_runtime.sh" "$TMP/out"
get "$MODEL_URL" "$MODEL_SHA" "$TMP/dl/model.tar.bz2"

# extract only what the page needs (data filter: no absolute paths, links or devices)
python3 -I - "$TMP/dl/model.tar.bz2" "$TMP/dl/m" <<'EOF'
import sys, tarfile
want = ('tokens.txt', 'en.phone', 'encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx',
        'decoder-epoch-13-avg-2-chunk-16-left-64.onnx', 'joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx')
with tarfile.open(sys.argv[1], 'r:bz2') as t:
    ms = [m for m in t.getmembers() if m.isfile() and m.name.split('/')[-1] in want and m.name.count('/') == 1]
    for m in ms:
        m.name = m.name.split('/')[-1]
    t.extractall(sys.argv[2], members=ms, filter='data')
EOF
M="$TMP/dl/m"
check() { got=$(sha256sum "$1" | cut -d' ' -f1); [ "$got" = "$2" ] || { echo "sha256 mismatch for $1" >&2; exit 1; }; }
check "$M/encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx" 408bbd740838c42d5bf6d1c5b80b3c88b616c7860b92d980328b5b068c76ae48
check "$M/decoder-epoch-13-avg-2-chunk-16-left-64.onnx" 63a22dd60f40fff082ac3e09afa507f6787da36df76ded2fbe145fa233e22c21
check "$M/joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx" 190d4067b4cc20b72a42a1916e69d92052000fb7051a427ebb1bc72a69207dc1
check "$M/tokens.txt" 2d3f32311f9b692b964da3c90e830258d3e78e013cb0c992dbfb15cd5a1a71b0
check "$M/en.phone" f7000ec3a90544c0c7c16090d8951779c2b322e14dad5006290f498567d439ea
cp "$M/encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx" "$TMP/out/kws-encoder.onnx"
cp "$M/decoder-epoch-13-avg-2-chunk-16-left-64.onnx" "$TMP/out/kws-decoder.onnx"
cp "$M/joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx" "$TMP/out/kws-joiner.onnx"
cp "$M/tokens.txt" "$TMP/out/kws-tokens.txt"
python3 -I "$HERE/make_keywords.py" "$M" "$TMP/out"      # one kws-words-<wake word>.txt per wake word

{ echo "Fetched $(date -u +%Y-%m-%dT%H:%M:%SZ) by tools/fetch_assets.sh"
  cat "$TMP/out/RUNTIME-MANIFEST.txt"
  echo "model:   $MODEL_URL sha256 $MODEL_SHA (Apache-2.0)"
  (cd "$TMP/out" && sha256sum *); } > "$TMP/out/MANIFEST.txt"
mkdir -p "$OUT"
cp "$TMP/out/"* "$OUT/"
echo "static/vendor/ ready: $(du -sh "$OUT" | cut -f1)"
