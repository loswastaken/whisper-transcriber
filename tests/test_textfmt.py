import unittest

from app.textfmt import build_paragraphs, build_srt, build_txt, format_timestamp


class FormatTimestampTest(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(format_timestamp(0), "00:00:00,000")

    def test_basic(self):
        self.assertEqual(format_timestamp(61.5), "00:01:01,500")

    def test_rounding(self):
        self.assertEqual(format_timestamp(1.0004), "00:00:01,000")
        self.assertEqual(format_timestamp(1.0006), "00:00:01,001")
        self.assertEqual(format_timestamp(59.9996), "00:01:00,000")

    def test_over_one_hour(self):
        self.assertEqual(format_timestamp(3600), "01:00:00,000")
        self.assertEqual(format_timestamp(3725.25), "01:02:05,250")
        self.assertEqual(format_timestamp(36000 + 1.5), "10:00:01,500")

    def test_negative_and_nan(self):
        self.assertEqual(format_timestamp(-3), "00:00:00,000")
        self.assertEqual(format_timestamp(float("nan")), "00:00:00,000")


class SrtTest(unittest.TestCase):
    def test_numbering_skips_empty(self):
        srt = build_srt([(0, 1.5, " Hello "), (1.5, 2, "  "), (2, 3725.25, "World\n again")])
        self.assertEqual(
            srt,
            "1\n00:00:00,000 --> 00:00:01,500\nHello\n\n"
            "2\n00:00:02,000 --> 01:02:05,250\nWorld again\n",
        )

    def test_end_before_start(self):
        srt = build_srt([(5, 4, "x")])
        self.assertIn("00:00:05,000 --> 00:00:05,000", srt)

    def test_empty(self):
        self.assertEqual(build_srt([]), "")


class ParagraphTest(unittest.TestCase):
    def test_joins_close_segments(self):
        segs = [(0, 1, " One."), (1.2, 2, "Two"), (2.1, 3, "three.")]
        self.assertEqual(build_txt(segs), "One. Two three.\n")

    def test_gap_starts_new_paragraph(self):
        segs = [(0, 1, "One."), (5, 6, "Two.")]
        self.assertEqual(build_paragraphs(segs), ["One.", "Two."])
        self.assertEqual(build_txt(segs), "One.\n\nTwo.\n")

    def test_long_paragraph_breaks_at_sentence(self):
        sentence = "word " * 30 + "end."
        segs = [(i, i + 1, sentence) for i in range(10)]
        paras = build_paragraphs(segs)
        self.assertGreater(len(paras), 1)
        for p in paras[:-1]:
            self.assertTrue(p.endswith("."))

    def test_hard_limit_without_punctuation(self):
        segs = [(i, i + 1, "x" * 100) for i in range(40)]
        paras = build_paragraphs(segs)
        self.assertGreater(len(paras), 1)
        self.assertTrue(all(len(p) < 1700 for p in paras))

    def test_empty(self):
        self.assertEqual(build_txt([]), "")
        self.assertEqual(build_txt([(0, 1, "  ")]), "")


if __name__ == "__main__":
    unittest.main()
