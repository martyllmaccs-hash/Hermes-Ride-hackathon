"""Installer guard integration tests; no network or SDK required."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'tools/build_wake_runtime.sh'


class RuntimeBuildGuards(unittest.TestCase):
    def run_helper(self, sdk_version, payload):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            tools = tmp / 'bin'
            tools.mkdir()
            # Replace network/compiler boundaries only; execute the actual helper,
            # sha256sum validation and extraction gating.
            for name, body in {
                'emcc': f'#!/bin/sh\necho "emcc {sdk_version}"\n',
                'curl': '#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\nprintf "%s" "$PAYLOAD" > "$2"\n',
                'tar': '#!/bin/sh\ntouch "$EXTRACTED"\nexit 99\n',
            }.items():
                p = tools / name
                p.write_text(body)
                p.chmod(0o755)
            out = tmp / 'output'
            env = dict(os.environ, PATH=f'{tools}:/usr/bin:/bin', PAYLOAD=payload,
                       EXTRACTED=str(tmp / 'extracted'), TMPDIR=str(tmp))
            result = subprocess.run(['sh', str(HELPER), str(out)], env=env,
                                    capture_output=True, text=True)
            return result, out.exists(), (tmp / 'extracted').exists()

    def test_wrong_sdk_rejected_before_download_or_output(self):
        result, output, extracted = self.run_helper('3.1.0', 'not source')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('4.0.23 required', result.stderr)
        self.assertFalse(output)
        self.assertFalse(extracted)

    def test_source_checksum_rejected_before_extraction_or_output(self):
        result, output, extracted = self.run_helper('4.0.23', 'corrupted archive')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('FAILED', result.stdout)
        self.assertFalse(output)
        self.assertFalse(extracted)
