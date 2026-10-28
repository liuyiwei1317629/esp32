import unittest

import screen_hub as sh


class FakeClock(object):
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class HubTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.hub = sh.ScreenHub(lib=None, clock=self.clock, rng=sh.random.Random(7))

    def test_starts_breathing(self):
        self.assertEqual(self.hub.anim, sh.BREATHING)
        self.assertEqual(self.hub.state, "idle")
        self.assertIn("按一下", self.hub.hint)

    def test_turn_states(self):
        self.hub.on_key()
        self.assertEqual(self.hub.state, "recording")
        self.hub.turn_begin()
        self.assertEqual(self.hub.state, "thinking")
        self.hub.first_audio()
        self.assertEqual(self.hub.state, "playing")
        self.assertEqual(self.hub.anim, sh.PLAYING)
        self.hub.turn_end()
        self.assertEqual(self.hub.state, "idle")
        self.assertEqual(self.hub.anim, sh.BREATHING)

    def test_turn_error_keeps_error_face(self):
        self.hub.turn_begin()
        self.hub.turn_end(ok=False)
        self.assertEqual(self.hub.state, "error")
        self.assertEqual(self.hub.anim, sh.ERROR)

    def test_key_always_switches_to_recording(self):
        self.hub.on_key()
        self.assertEqual(self.hub.state, "recording")
        self.assertIn("听", self.hub.hint)

    def test_idle_insert_only_after_enter_window(self):
        self.clock.t = 30
        self.hub.tick()
        self.assertFalse(self.hub.idle_active)
        self.assertEqual(self.hub.anim, sh.BREATHING)
        self.clock.t = 130
        self.hub.tick()
        self.assertTrue(self.hub.idle_active)
        self.assertIn(self.hub.anim, sh.IDLE_POOL)

    def test_one_shot_returns_to_breathing(self):
        self.clock.t = 130
        self.hub.tick()
        first = self.hub.anim
        self.assertIn(first, sh.IDLE_POOL)
        self.clock.t = 130 + 6
        self.hub.tick()
        self.assertEqual(self.hub.anim, sh.BREATHING)

    def test_insert_repeats_every_3_to_5_minutes(self):
        self.clock.t = 130
        self.hub.tick()
        first = self.hub.anim
        self.clock.t = 130 + 179              # 3 分钟内不该再来一个
        self.hub.tick()
        self.assertEqual(self.hub.anim, sh.BREATHING)
        self.clock.t = 130 + 301              # 5 分钟上限之后必然已经插过
        self.hub.tick()
        self.assertIn(self.hub.anim, sh.IDLE_POOL)
        self.assertNotEqual(self.hub.anim, first)   # 30 分钟内不重复

    def test_key_after_idle_plays_reaction_sometimes(self):
        played = 0
        for seed in range(50):
            clock = FakeClock()
            hub = sh.ScreenHub(lib=None, clock=clock, rng=sh.random.Random(seed))
            clock.t = 200
            hub.tick()
            hub.on_key()
            if hub.anim in sh.KEY_POOL:
                played += 1
        ratio = played / 50.0
        self.assertGreater(ratio, 0.4)
        self.assertLess(ratio, 0.8)

    def test_first_key_never_gets_a_reaction(self):
        self.hub.on_key()                     # 刚开机（没空闲过）
        self.assertEqual(self.hub.anim, sh.LISTENING)

    def test_recording_settles_to_listening_after_reaction(self):
        for seed in range(20):
            clock = FakeClock()
            hub = sh.ScreenHub(lib=None, clock=clock, rng=sh.random.Random(seed))
            clock.t = 200
            hub.tick()
            hub.on_key()
            if hub.anim in sh.KEY_POOL:
                clock.t = 203
                hub.tick()
                self.assertEqual(hub.anim, sh.LISTENING)
                self.assertEqual(hub.state, "recording")
                return
        self.fail("20 个种子都没触发按键反应，概率逻辑不对")

    def test_recording_resets_if_no_turn_arrives(self):
        self.hub.on_key()
        self.clock.t = 10
        self.hub.tick()
        self.assertEqual(self.hub.state, "recording")
        self.clock.t = 25                                  # 超过 20 s 还没 /turn
        self.hub.tick()
        self.assertEqual(self.hub.state, "idle")

    def test_frame_jpeg_is_320x240(self):
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(self.hub.frame_jpeg()))
        self.assertEqual(im.size, (320, 240))
        self.assertEqual(self.hub.state, "idle")


if __name__ == "__main__":
    unittest.main()
