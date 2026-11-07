"""Execute the real React card against a simulated SDK. Not an installed-host test."""
import re
import subprocess
import unittest
from pathlib import Path

# Guard against a silently shrinking suite: the card's behavior contract has
# only grown, so a run reporting fewer scenarios than this is a regression.
MINIMUM_SCENARIOS = 25


class DesktopFlowTests(unittest.TestCase):
    def test_react_interactions(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ['node', 'tests/desktop_seller_card.test.mjs'],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        match = re.search(r'^(\d+)/(\d+) passed', result.stdout, re.MULTILINE)
        if match is None:
            self.fail(f'no summary line in:\n{result.stdout}')
        passed, total = int(match.group(1)), int(match.group(2))
        self.assertEqual(passed, total, f'{total - passed} scenario(s) failed:\n{result.stdout}')
        self.assertGreaterEqual(total, MINIMUM_SCENARIOS, 'scenario count dropped')
        self.assertNotIn('FAIL ', result.stdout)


if __name__ == '__main__':
    unittest.main()
