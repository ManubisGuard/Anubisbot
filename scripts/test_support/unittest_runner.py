"""Unittest discovery with a hard per-test timeout for isolated local tests."""
import os
import signal
import sys
import unittest


def _timeout_seconds() -> int:
    try:
        value = int(os.environ.get("TEST_TIMEOUT_SECONDS", "20"))
    except ValueError:
        value = 20
    return max(1, min(value, 300))


TEST_TIMEOUT = _timeout_seconds()


class PerTestTimeoutResult(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        signal.signal(signal.SIGALRM, self._on_timeout)
        signal.setitimer(signal.ITIMER_REAL, TEST_TIMEOUT)

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        super().stopTest(test)

    @staticmethod
    def _on_timeout(_signum, _frame):
        raise TimeoutError(f"test exceeded {TEST_TIMEOUT}s per-test timeout")


class PerTestTimeoutRunner(unittest.TextTestRunner):
    resultclass = PerTestTimeoutResult


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: unittest_runner.py TEST_DIRECTORY", file=sys.stderr)
        return 2
    start_dir = os.path.abspath(sys.argv[1])
    suite = unittest.defaultTestLoader.discover(start_dir, pattern="test*.py", top_level_dir=start_dir)
    result = PerTestTimeoutRunner(verbosity=2, buffer=False).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
