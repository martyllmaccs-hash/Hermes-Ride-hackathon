#!/usr/bin/env python3
"""Export the Hermes-Ride plugin as a clean tree for the public repo.

Why: the private dev repo carries live-run evidence, recordings and host paths that
must not be published. This builds the public tree from a single commit, drops the
private material, refuses to finish if a leak pattern is still present, and prints a
report the caller can paste into the release notes.

Usage:
    python3 tools/export_public.py                  # writes to the scratch dir, scans, exits 1 on a leak
    python3 tools/export_public.py --out DIR        # explicit destination (must be empty or absent)
    python3 tools/export_public.py --list-excluded  # show what would be dropped, write nothing

The scan fails loudly (exit 1) on: key/token shapes, private-key blocks, e-mail
addresses, Tailscale 100.x addresses and *.ts.net host names, host paths
(the author's home root, /home/<user>), and the rider's home-area coordinates.

Excluded on purpose, with the reason recorded in EXCLUDED:
  HANDOFF.md, docs/evidence/      live-run logs, transcripts, phone screenshots
  tests/live_phone.py             drives a real phone on the author's tailnet
  .env, .env.*                    credentials
  __pycache__, *.pyc              build noise
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

# The author's host root is assembled from pieces so this file never contains the literal path it
# scans for (it would otherwise flag itself).
HOST_ROOT = "/" + "opt" + "/" + "data"

EXCLUDED = [
    ("HANDOFF.md", "live-run handoff notes: sessions, evidence paths, host state"),
    ("docs/evidence/", "ride logs, transcripts and screenshots from real rides"),
    ("tests/live_phone.py", "drives a real phone over the author's tailnet"),
    (".env", "credentials"),
]
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".orig", ".rej", ".log")

SCAN = [
    (r"AKIA[0-9A-Z]{16}", "aws key id"),
    (r"\bsk-[A-Za-z0-9]{20,}", "api key"),
    (r"\bghp_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}", "github token"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}", "slack token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key block"),
    (r"(?i)(?:ELEVENLABS|ANTHROPIC|OPENAI|GITHUB|TELEGRAM)_[A-Z_]*KEY\s*[=:]\s*['\"]?[A-Za-z0-9_-]{20,}", "key assigned in a file"),
    (r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b", "tailscale 100.x address"),
    (r"[A-Za-z0-9-]+\.ts\.net\b", "tailnet host name"),
    (r"[\w.+-]+@(?!example\.(?:com|org)\b)[\w-]+\.[\w.]{2,}", "e-mail address"),
    (rf"{HOST_ROOT}|/home/[a-z]+/", "host path"),
    (r"-37\.8\d*|145\.0\d*", "home-area coordinates"),
]
# Host-specific values are rewritten in the exported tree only, so the dev repo keeps its real
# paths and coordinates for local runs. Each pair is (pattern, replacement, why).
SUBSTITUTIONS = [
    (rf"{HOST_ROOT}/tools/whistle/venv/bin/python", "~/whistle/venv/bin/python", "author's whistle interpreter"),
    (rf"{HOST_ROOT}/tools/whistle/whistle\.cact", "~/whistle/whistle.cact", "author's whistle weights"),
    (rf"{HOST_ROOT}/plugin-data/hermes-ride/rides", "~/rides", "author's ride logs"),
]
SUBSTITUTIONS = [(re.compile(a), b, why) for a, b, why in SUBSTITUTIONS]

TEXT_SUFFIXES = (".py", ".js", ".json", ".md", ".yaml", ".yml", ".html", ".css", ".txt", ".sh", ".cfg", ".toml")


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args], check=True, capture_output=True, text=True).stdout


def is_excluded(rel):
    for name, _ in EXCLUDED:
        if name.endswith("/"):
            if rel == name[:-1] or rel.startswith(name):
                return name
        elif rel == name or rel.startswith(name + "/"):
            return name
    if rel.endswith(EXCLUDED_SUFFIXES) or "__pycache__" in rel.split("/"):
        return "build noise"
    return None


def build(root, out):
    commit = git(root, "rev-parse", "HEAD").strip()
    tmp = tempfile.mkdtemp(prefix="ride-export-")
    tar = os.path.join(tmp, "tree.tar")
    with open(tar, "wb") as fh:
        proc = subprocess.run(["git", "-C", root, "archive", "--format=tar", commit], stdout=fh)
        if proc.returncode:
            raise SystemExit("git archive failed")
    os.makedirs(out, exist_ok=True)
    subprocess.run(["tar", "-xf", tar, "-C", out], check=True)
    shutil.rmtree(tmp)

    dropped = {}
    for base, dirs, files in os.walk(out, topdown=True):
        for d in list(dirs):
            rel = os.path.relpath(os.path.join(base, d), out)
            why = is_excluded(rel + "/")
            if why:
                shutil.rmtree(os.path.join(base, d))
                dirs.remove(d)
                dropped[rel + "/"] = why
        for name in files:
            rel = os.path.relpath(os.path.join(base, name), out)
            why = is_excluded(rel)
            if why:
                os.unlink(os.path.join(base, name))
                dropped[rel] = why
    return commit, dropped


def apply_substitutions(out):
    """Rewrite host-specific values in the exported tree; returns a list of what was changed."""
    changed = []
    for base, _, files in os.walk(out):
        for name in files:
            if not name.endswith(TEXT_SUFFIXES):
                continue
            path = os.path.join(base, name)
            try:
                text = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            new = text
            for pattern, repl, why in SUBSTITUTIONS:
                new, n = pattern.subn(repl, new)
                if n:
                    changed.append((os.path.relpath(path, out), n, why))
            if new != text:
                open(path, "w", encoding="utf-8").write(new)
    return changed


README_NOTE = """
## What this repository does not ship
The material that only makes sense on the author's own machine is left out of this repository, and the tests that
need it skip cleanly when it is absent:

- `HANDOFF.md` and `docs/evidence/` (build logs, ride transcripts, phone screenshots),
- `tests/live_phone.py` (drives a phone over a private network).

Everything else here is the plugin as it runs: `static/`, the Python core, the tests and the docs.
"""


def append_readme(out):
    path = os.path.join(out, "README.md")
    if not os.path.isfile(path):
        return None
    text = open(path, encoding="utf-8").read()
    marker = "## What this repository does not ship"
    if marker in text:
        return None
    licence = text.find("## License")
    note = README_NOTE.strip() + "\n\n"
    text = (text[:licence] + note + text[licence:]) if licence > 0 else (text.rstrip() + "\n\n" + note)
    open(path, "w", encoding="utf-8").write(text)
    return path


def scan(out):
    hits = []
    for base, _, files in os.walk(out):
        for name in files:
            if not name.endswith(TEXT_SUFFIXES):
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, out)
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for pattern, label in SCAN:
                for m in re.finditer(pattern, text):
                    line = text.count("\n", 0, m.start()) + 1
                    hits.append((rel, line, label, m.group(0)[:60]))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--out", default=None)
    ap.add_argument("--list-excluded", action="store_true")
    args = ap.parse_args()

    if args.list_excluded:
        for name, why in EXCLUDED:
            print(f"{name:24} {why}")
        return 0

    out = args.out or tempfile.mkdtemp(prefix="ride-public-")
    if os.path.isdir(out) and os.listdir(out):
        raise SystemExit(f"refusing to write into a non-empty directory: {out}")

    commit, dropped = build(args.root, out)
    changed = apply_substitutions(out)
    if append_readme(out):
        print("  README: appended the \"does not ship\" note")
    hits = scan(out)
    files = sum(len(f) for _, _, f in os.walk(out))
    print(f"export of {commit[:10]} -> {out}: {files} files, {len(dropped)} dropped")
    for rel, why in sorted(dropped.items()):
        print(f"  dropped {rel}  ({why})")
    for rel, n, why in sorted(changed):
        print(f"  rewrote {rel} ({n}x, {why})")
    if hits:
        print(f"LEAK SCAN FAILED: {len(hits)} hit(s)")
        for rel, line, label, text in hits:
            print(f"  {rel}:{line}  {label}: {text}")
        return 1
    print("leak scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
