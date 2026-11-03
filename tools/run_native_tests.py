"""Run the native core unittest cases against Binary Ninja's real metadata and settings.

PYTHONPATH='/Applications/Binary Ninja.app/Contents/Resources/python' \
    .venv/bin/python tools/run_native_tests.py
"""

import io
import json
import sys
import unittest
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    output = root / '.test-artifacts' / 'native-core-tests'
    output.mkdir(parents=True, exist_ok=True)
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(root / 'tests' / 'native_core'),
                                                top_level_dir=str(root))
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    text = stream.getvalue()
    (output / 'results.txt').write_text(text)
    print(text)
    ok = result.wasSuccessful() and result.testsRun > len(result.skipped)
    (output / 'result.json').write_text(json.dumps({
        'ok': ok, 'tests': result.testsRun, 'skipped': len(result.skipped),
        'failures': len(result.failures), 'errors': len(result.errors)}, indent=2))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
