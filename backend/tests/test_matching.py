import unittest

from app.matching import add_hit, clock_label, normalize_plate, plates_match


class MatchingTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_plate(" ab-12 cde "), "AB12CDE")

    def test_match_one_edit(self):
        self.assertTrue(plates_match("AB12CDE", "AB12CDF"))
        self.assertFalse(plates_match("AB12CDE", "ZZ99ZZZ"))

    def test_group_gap(self):
        tracks = []
        add_hit(tracks, time=1.0, score=0.5, label="face", payload=1, gap=1.0)
        add_hit(tracks, time=1.5, score=0.9, label="face", payload=2, gap=1.0)
        add_hit(tracks, time=4.0, score=0.4, label="face", payload=3, gap=1.0)
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].payload, 2)
        self.assertEqual(tracks[0].start, 1.0)
        self.assertEqual(tracks[0].end, 1.5)
        self.assertEqual(tracks[1].payload, 3)

    def test_separate_plates_in_one_frame(self):
        tracks = []
        add_hit(tracks, time=1, score=0.5, label="AAA111", payload="A", gap=1.5)
        add_hit(tracks, time=1, score=0.5, label="BBB222", payload="B", gap=1.5)
        add_hit(tracks, time=2, score=0.8, label="AAA111", payload="A2", gap=1.5)
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].payload, "A2")
        self.assertEqual(tracks[0].end, 2)

    def test_clock(self):
        self.assertEqual(clock_label(65), "1:05")
        self.assertEqual(clock_label(3661), "1:01:01")


if __name__ == "__main__":
    unittest.main()
