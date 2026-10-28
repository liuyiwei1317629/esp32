import os
import unittest

import screen_config as sc


class ConfigTests(unittest.TestCase):
    def test_defaults_are_sane(self):
        self.assertGreaterEqual(sc.FPS, 5)
        self.assertLessEqual(sc.FPS, 15)
        self.assertGreaterEqual(sc.QUALITY, 60)
        self.assertLessEqual(sc.QUALITY, 90)

    def test_idle_windows_are_ordered(self):
        lo, hi = sc.IDLE_ENTER_S
        self.assertLess(lo, hi)
        lo, hi = sc.IDLE_EVERY_S
        self.assertLess(lo, hi)
        self.assertEqual((lo, hi), (180, 300))

    def test_reaction_probability_is_sixty_percent(self):
        self.assertEqual(sc.KEY_REACTION_PROB, 0.6)

    def test_paths_exist(self):
        self.assertTrue(os.path.isdir(sc.FRAMES_DIR))
        self.assertTrue(os.path.isfile(sc.FONT_PATH))
        self.assertTrue(os.path.isfile(sc.OFFLINE_JPG))

    def test_disabled_by_default(self):
        self.assertFalse(sc.SCREEN_ENABLED)


if __name__ == "__main__":
    unittest.main()
