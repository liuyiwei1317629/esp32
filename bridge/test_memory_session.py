import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import memory_store


class ParseSessionIdTest(unittest.TestCase):
    def test_plain_value(self):
        self.assertEqual(memory_store.parse_session_id("session_id=dev1"), "dev1")

    def test_value_is_url_decoded(self):
        self.assertEqual(
            memory_store.parse_session_id("session_id=a%2Fb%20c"), "a/b c")

    def test_first_value_wins_and_extra_params_are_ignored(self):
        query = "x=1&session_id=dev1&session_id=dev2"
        self.assertEqual(memory_store.parse_session_id(query), "dev1")

    def test_fifty_five_chars_kept_and_longer_values_truncated(self):
        value = "a" * 55
        self.assertEqual(memory_store.parse_session_id("session_id=" + value), value)
        self.assertEqual(
            memory_store.parse_session_id("session_id=" + "a" * 80), "a" * 55)

    def test_empty_or_missing_falls_back_to_anon(self):
        self.assertEqual(memory_store.parse_session_id(""), "anon")
        self.assertEqual(memory_store.parse_session_id("x=1"), "anon")
        self.assertEqual(memory_store.parse_session_id("session_id="), "anon")
        self.assertEqual(memory_store.parse_session_id("session_id=%20"), " ")

    def test_accepts_a_full_path_query_string(self):
        self.assertEqual(
            memory_store.parse_session_id("/turn?session_id=dev1"), "dev1")


class SessionBucketTest(unittest.TestCase):
    def test_hash_is_stable_and_short(self):
        bucket = memory_store.session_bucket("dev1")
        self.assertEqual(bucket, hashlib.sha1(b"dev1").hexdigest()[:16])
        self.assertEqual(len(bucket), 16)

    def test_path_like_values_do_not_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = memory_store.MemoryStore(tmp)
            path = store.session_path("../" + "x" * 80)
            self.assertEqual(os.path.dirname(path), store.sessions_dir)
            self.assertTrue(os.path.basename(path).endswith(".json"))
            self.assertNotIn("..", os.path.basename(path))

    def test_empty_value_uses_anon(self):
        self.assertEqual(memory_store.session_bucket(""), memory_store.session_bucket("anon"))


class NormalizeAndIdTest(unittest.TestCase):
    def test_normalizes_width_punctuation_and_spaces(self):
        self.assertEqual(memory_store.normalize_text("  我 喜欢，香菜。 "), "我 喜欢香菜")

    def test_same_normalized_text_has_same_id(self):
        one = memory_store.record_id("preference", "我喜欢香菜")
        two = memory_store.record_id("preference", " 我喜欢，香菜。 ")
        self.assertEqual(one, two)

    def test_kind_is_part_of_id(self):
        self.assertNotEqual(memory_store.record_id("fact", "同一句话"),
                            memory_store.record_id("name", "同一句话"))


if __name__ == "__main__":
    unittest.main()
