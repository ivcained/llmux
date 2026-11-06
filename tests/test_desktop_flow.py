"""Execute the real React card against a simulated SDK. Not an installed-host test."""
import subprocess
import unittest
from pathlib import Path

class DesktopFlowTests(unittest.TestCase):
    def test_react_interactions(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['node', 'tests/desktop_seller_card.test.mjs'], cwd=root, text=True, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('17/17 passed', result.stdout)

if __name__ == '__main__':
    unittest.main()
