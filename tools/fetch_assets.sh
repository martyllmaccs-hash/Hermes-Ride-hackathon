#!/bin/sh
# Fetch the browser wake-word assets into static/vendor/ (gitignored). Every download is pinned by URL and sha256;
# a mismatch aborts and leaves static/vendor/ untouched. Needs: curl, sha256sum, python3 (tarfile, for .tar.bz2).
#
#   sh tools/fetch_assets.sh            # ~33 MB download, ~16 MB kept in static/vendor/
#
# 1. sherpa-onnx WASM keyword-spotting runtime (Apache-2.0): the upstream wasm/kws build target
#    (k2-fsa/sherpa-onnx wasm/kws, sherpa-onnx-wasm-kws-main.{js,wasm} + sherpa-onnx-kws.js). Upstream publishes no
#    prebuilt KWS web build any more; this pin is the copy hosted in the Hugging Face space yuiyide/sherpa-oon-kws at
#    commit 6a6f3379ba6591bb17baf95537f278d1078d4d53 (uploaded 2026-02-26, third party). To build your own instead:
#    sherpa-onnx ./build-wasm-simd-kws.sh, then copy the same three files and set SHERPA_SKIP_RUNTIME=1.
# 2. Model sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20 (release kws-models; Apache-2.0 per the original
#    ModelScope card pkufool/icefall-kws-zipformer-zh-en-3M-2025-12-20): int8 encoder/joiner, fp32 decoder, tokens.
# 3. kws-words-<wake word>.txt (one per ride_logic.WAKE_WORDS entry) generated from the model's own lexicon (tools/make_keywords.py).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
OUT="$ROOT/static/vendor"
HF=https://huggingface.co/spaces/yuiyide/sherpa-oon-kws/resolve/6a6f3379ba6591bb17baf95537f278d1078d4d53
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

if [ "${SHERPA_SKIP_RUNTIME:-0}" != "1" ]; then
  get "$HF/sherpa-onnx-wasm-kws-main.js" a01e1919c4be1f8a366b8b48724b9511a0fdb63af09de71f63232ced899abd24 "$TMP/out/sherpa-onnx-wasm-kws-main.js"
  get "$HF/sherpa-onnx-wasm-kws-main.wasm" 69eabc3529eb0475b9e777689c4a8de5dfd483375a4287d8b2bf98825e2aebfc "$TMP/out/sherpa-onnx-wasm-kws-main.wasm"
  get "$HF/sherpa-onnx-kws.js" 4777ae0dfbac1f59ae52b70d8751cb497f299bca204affe999d16546ae3cdba8 "$TMP/out/sherpa-onnx-kws.js"
else
  for f in sherpa-onnx-wasm-kws-main.js sherpa-onnx-wasm-kws-main.wasm sherpa-onnx-kws.js; do
    [ -f "$OUT/$f" ] || { echo "SHERPA_SKIP_RUNTIME=1 but $OUT/$f is missing" >&2; exit 1; }
    cp "$OUT/$f" "$TMP/out/"
  done
fi
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
  echo "runtime: $HF (sherpa-onnx wasm/kws, Apache-2.0)"
  echo "model:   $MODEL_URL sha256 $MODEL_SHA (Apache-2.0)"
  (cd "$TMP/out" && sha256sum *); } > "$TMP/out/MANIFEST.txt"
mkdir -p "$OUT"
cp "$TMP/out/"* "$OUT/"
echo "static/vendor/ ready: $(du -sh "$OUT" | cut -f1)"
