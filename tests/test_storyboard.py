"""分镜结构、时间轴与默认值。不需要 API key，也不需要 ffmpeg。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from vslib import storyboard as sb  # noqa: E402


class TestDefaults(unittest.TestCase):
    def test_effects_default_to_off(self):
        s = sb.Storyboard()
        self.assertFalse(s.audio)
        self.assertFalse(s.voiceover)
        self.assertEqual(s.transition, "none")
        self.assertEqual(s.bgm, "")
        self.assertEqual(s.style, "")

    def test_load_minimal_json(self):
        payload = {
            "title": "示例",
            "ratio": "9:16",
            "resolution": "1080p",
            "shots": [
                {"id": "s1", "duration": 5, "prompt": "推门"},
                {"id": "s2", "duration": 5, "prompt": "倒咖啡"},
            ],
        }
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "storyboard.json"
            p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            s = sb.load(p)
        self.assertEqual(len(s.shots), 2)
        self.assertEqual(s.ratio, "9:16")
        self.assertEqual(s.total_duration, 10)
        self.assertEqual(s.shots[0].prompt, "推门")


class TestTimeline(unittest.TestCase):
    def _sb(self):
        s = sb.Storyboard()
        s.shots = [
            sb.Shot(id="s1", prompt="x", duration=5, subtitle="第一条字幕"),
            sb.Shot(id="s2", prompt="y", duration=5, subtitle="第二条字幕"),
        ]
        return s

    def test_no_overlap(self):
        cues = sb.timeline(self._sb(), 0.0)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0][0], 0.0)
        self.assertAlmostEqual(cues[1][0], 5.0)

    def test_overlap_compresses_axis(self):
        cues = sb.timeline(self._sb(), 0.5)
        self.assertAlmostEqual(cues[1][0], 4.5)
        self.assertLess(cues[1][0], 5.0)

    def test_split_caption_fills_span(self):
        text = "很多人以为做视频最难的是设备。其实不是。最难的是想清楚你要说什么。设备可以租，想法租不到。"
        parts = sb._split_caption(text, 5.0)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(t.strip() for t, _ in parts))
        self.assertAlmostEqual(sum(d for _, d in parts), 5.0, places=6)

    def test_validate_flags_long_voiceover(self):
        s = sb.Storyboard()
        s.shots = [sb.Shot(id="s1", prompt="x", duration=5,
                           voiceover="一段特别长的旁白文案" * 8)]
        self.assertTrue(sb.validate(s))


if __name__ == "__main__":
    unittest.main()
