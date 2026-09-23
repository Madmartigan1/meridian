"""Shared runner for the standalone test scripts.

No test framework is required. Each test file collects its own `test_*`
functions and hands them here, so the reporting is identical across files and a
failure exits non-zero for CI.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run(namespace):
    """Run every `test_*` callable in `namespace`. Returns a process exit code."""
    tests = [v for k, v in sorted(namespace.items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print("  PASS  %s" % fn.__name__)
        except AssertionError as exc:
            failures += 1
            print("  FAIL  %s: %s" % (fn.__name__, exc))
        except Exception as exc:  # noqa: BLE001 - a crash is a failure too
            failures += 1
            print("  ERROR %s: %s: %s" % (fn.__name__, type(exc).__name__, exc))
    print("\n%d/%d passed" % (len(tests) - failures, len(tests)))
    return 1 if failures else 0


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol
