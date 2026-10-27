import ctypes
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import winperf


class WinperfTest(unittest.TestCase):
    def test_struct_matches_the_api_contract(self):
        # SetProcessInformation rejects anything but a 12-byte, version-1 state.
        self.assertEqual(winperf._CURRENT_VERSION, 1)
        self.assertEqual(winperf._PROCESS_POWER_THROTTLING, 4)
        self.assertEqual(ctypes.sizeof(winperf._ThrottlingState()), 12)

    def test_unthrottle_returns_bool_and_is_idempotent(self):
        first = winperf.unthrottle()
        second = winperf.unthrottle()
        self.assertIsInstance(first, bool)
        self.assertIsInstance(second, bool)
        if sys.platform == "win32":
            self.assertTrue(first, "SetProcessInformation refused the state")
        else:
            self.assertFalse(first)


if __name__ == "__main__":
    unittest.main()
