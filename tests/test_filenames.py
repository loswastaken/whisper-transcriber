import unittest

from app.filenames import (content_disposition, download_filename, is_valid_job_id,
                           sanitize_display_name, upload_extension)


class ExtensionTest(unittest.TestCase):
    def test_allowed(self):
        for name in ["a.mp3", "b.MP4", "c.wav", "d.m4a", "e.mkv", "f.webm", "g.ogg",
                     "h.FLAC", "i.mov", "j.aac", "k.Opus", "dir/sub/l.mp3", "C:\\x\\m.wav"]:
            self.assertIsNotNone(upload_extension(name), name)
        self.assertEqual(upload_extension("Song.MP3"), ".mp3")

    def test_rejected(self):
        for name in ["", None, "a.exe", "a.mp3.exe", "mp3", ".mp3x", "a.txt", "a.m3u8",
                     "a.html", "noext", "a.mp3/"]:
            self.assertIsNone(upload_extension(name), name)


class DisplayNameTest(unittest.TestCase):
    def test_strips_path_and_controls(self):
        self.assertEqual(sanitize_display_name("../../etc/pass\x00wd"), "pass wd")
        self.assertEqual(sanitize_display_name("C:\\Users\\me\\talk.mp3"), "talk.mp3")
        self.assertEqual(sanitize_display_name("a\r\nb\tc"), "a b c")
        self.assertEqual(sanitize_display_name("..hidden"), "hidden")

    def test_fallback(self):
        self.assertEqual(sanitize_display_name(""), "untitled")
        self.assertEqual(sanitize_display_name(None, "upload"), "upload")
        self.assertEqual(sanitize_display_name("   \x01 ", "upload"), "upload")

    def test_max_length(self):
        self.assertEqual(len(sanitize_display_name("x" * 500)), 200)

    def test_unicode_kept(self):
        self.assertEqual(sanitize_display_name("Café — interview.m4a"), "Café — interview.m4a")

    def test_download_filename(self):
        self.assertEqual(download_filename("talk.mp3", ".txt"), "talk.txt")
        self.assertEqual(download_filename("My video title", ".srt"), "My video title.srt")
        self.assertEqual(download_filename("", ".txt"), "transcript.txt")

    def test_content_disposition(self):
        header = content_disposition('Café "x";.txt')
        self.assertTrue(header.startswith('attachment; filename="Caf_ _x__.txt"'))
        self.assertIn("filename*=UTF-8''Caf%C3%A9%20%22x%22%3B.txt", header)
        self.assertNotIn("\n", header)


class JobIdTest(unittest.TestCase):
    def test_job_id(self):
        self.assertTrue(is_valid_job_id("0123456789abcdef0123456789abcdef"))
        for bad in ["", "0123456789ABCDEF0123456789ABCDEF", "../" + "a" * 29,
                    "a" * 31, "a" * 33, None, "g" * 32]:
            self.assertFalse(is_valid_job_id(bad), bad)


if __name__ == "__main__":
    unittest.main()
