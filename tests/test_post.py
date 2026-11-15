"""尺寸换算与字幕文件。不依赖 ffmpeg 二进制。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from vslib import post  # noqa: E402


class TestSizeFor(unittest.TestCase):
    RATIOS = {
        "16:9": 16 / 9,
        "9:16": 9 / 16,
        "1:1": 1.0,
        "4:3": 4 / 3,
        "3:4": 3 / 4,
        "21:9": 21 / 9,
    }

    def test_height_and_even_dimensions(self):
        for ratio, expected in self.RATIOS.items():
            for height in (720, 1080, 2160):
                w, h = post.size_for(ratio, height)
                with self.subTest(ratio=ratio, height=height):
                    # height 是「短边」：横屏时是高，竖屏时是宽
                    self.assertEqual(min(w, h), height)
                    self.assertEqual(w % 2, 0)
                    self.assertEqual(h % 2, 0)
                    self.assertAlmostEqual(w / h, expected, delta=0.01)

    def test_portrait_is_not_square(self):
        # 曾经的 bug：9:16 在 1080 档被算成 606x606
        w, h = post.size_for("9:16", 1080)
        self.assertNotEqual(w, h)
        self.assertEqual((w, h), (1080, 1920))

    def test_unknown_ratio_falls_back(self):
        w, h = post.size_for("不存在的比例", 1080)
        self.assertEqual((w, h), post.size_for("16:9", 1080))


class TestSrt(unittest.TestCase):
    def test_write_srt_format(self):
        cues = [(0.0, 2.5, "第一条"), (2.5, 5.0, "第二条")]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "subs.srt"
            post.write_srt(cues, p)
            text = p.read_text(encoding="utf-8")
        self.assertIn("00:00:00,000 --> 00:00:02,500", text)
        self.assertIn("00:00:02,500 --> 00:00:05,000", text)
        self.assertIn("第一条", text)
        self.assertEqual(text.count("-->"), 2)


if __name__ == "__main__":
    unittest.main()
