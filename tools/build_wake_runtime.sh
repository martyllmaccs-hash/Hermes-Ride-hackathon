#!/bin/sh
# Build Ride's browser KWS runtime locally from checksum-pinned official source.
# Usage: sh tools/build_wake_runtime.sh OUTPUT_DIRECTORY
# Activate official emsdk 4.0.23 first. CMake/make/curl/python3/sha256sum required.
set -eu
OUT=${1:?Usage: build_wake_runtime.sh OUTPUT_DIRECTORY}
command -v emcc >/dev/null || { echo 'Activate official Emscripten SDK 4.0.23 first (see README).' >&2; exit 1; }
emcc --version | head -n 1 | grep -F '4.0.23' >/dev/null || { echo 'Emscripten 4.0.23 required' >&2; exit 1; }
REV=bec3acea719faafa6c86c7022d98f19663b82a76
SHA=64b9fd63efe0501a577bd6760c23b7892806115ab31a9051b68da1c63ec5da56
URL=https://codeload.github.com/k2-fsa/sherpa-onnx/tar.gz/$REV
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -fsSL --retry 2 --max-time 240 "$URL" -o "$TMP/source.tar.gz"
printf '%s  %s\n' "$SHA" "$TMP/source.tar.gz" | sha256sum -c -
tar -xzf "$TMP/source.tar.gz" -C "$TMP"
SRC="$TMP/sherpa-onnx-$REV"
# Ride injects its verified epoch-13 model and keywords at runtime, not the
# upstream epoch-12 Chinese demo. Disable demo preloading, its guard, and .data
# installation together. This avoids bundling a second model or faking a decoder.
python3 -I - "$SRC/wasm/kws/CMakeLists.txt" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
s = p.read_text()
start = s.index('if(NOT EXISTS ')
end = s.index('endif()', start) + len('endif()')
s = s[:start] + s[end:]
for line in (
    'string(APPEND MY_FLAGS "--preload-file ${CMAKE_CURRENT_SOURCE_DIR}/assets@. ")\n',
    '        "$<TARGET_FILE_DIR:sherpa-onnx-wasm-kws-main>/sherpa-onnx-wasm-kws-main.data"\n',
):
    if s.count(line) != 1:
        raise SystemExit('Pinned upstream CMake layout changed')
    s = s.replace(line, '')
p.write_text(s)
PY
(cd "$SRC" && bash ./build-wasm-simd-kws.sh)
BIN="$SRC/build-wasm-simd-kws/install/bin/wasm"
mkdir -p "$OUT"
for f in sherpa-onnx-wasm-kws-main.js sherpa-onnx-wasm-kws-main.wasm sherpa-onnx-kws.js; do
  test -s "$BIN/$f"
  cp "$BIN/$f" "$OUT/"
done
{ printf 'Locally built official source: %s sha256 %s\n' "$URL" "$SHA"
  printf 'Emscripten: 4.0.23; upstream demo preloading disabled; Ride supplies verified model files.\n'
  (cd "$OUT" && sha256sum sherpa-onnx*); } > "$OUT/RUNTIME-MANIFEST.txt"
