import unittest

from app.jobs import ScanError
from app.live_source import empty_note, parse_source


class LiveSourceTests(unittest.TestCase):
    def test_camera_number(self):
        parsed = parse_source(" 0 ")
        self.assertEqual(parsed.kind, "device")
        self.assertEqual(parsed.value, 0)
        self.assertEqual(parsed.label, "Camera 0")
        parsed = parse_source("camera:2")
        self.assertEqual(parsed.value, 2)
        self.assertEqual(parsed.label, "Camera 2")

    def test_camera_number_limit(self):
        self.assertEqual(parse_source("16").value, 16)
        with self.assertRaises(ScanError):
            parse_source("17")

    def test_rtsp_keeps_secret_out_of_the_label(self):
        raw = "rtsp://user:secret@10.0.0.8:554/live"
        parsed = parse_source(raw)
        self.assertEqual(parsed.kind, "url")
        self.assertEqual(parsed.value, raw)
        self.assertEqual(parsed.label, "rtsp://10.0.0.8:554/live")
        self.assertNotIn("secret", parsed.label)

    def test_http_and_ipv6(self):
        self.assertEqual(
            parse_source("http://cam.local/video.mjpg").label,
            "http://cam.local/video.mjpg",
        )
        self.assertEqual(parse_source("rtsp://[::1]:554/live").label, "rtsp://[::1]:554/live")

    def test_rejects_files_and_empty(self):
        for value in ("", "   ", "clip.mp4", "file:///etc/passwd", "ftp://cam/live", "rtsp://", "camera:"):
            with self.assertRaises(ScanError):
                parse_source(value)

    def test_empty_note(self):
        self.assertIn("similarity floor", empty_note("faces", ""))
        self.assertIn("AB12", empty_note("plates", "AB12"))
        self.assertIn("license plate", empty_note("both", ""))


if __name__ == "__main__":
    unittest.main()
