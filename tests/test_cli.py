"""CLI 注册、默认值与授权约束。不触发任何真实 API 调用。"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("vs_under_test", SCRIPTS / "vs.py")
vs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vs)

from vslib.core import Config  # noqa: E402
from vslib.providers import PROVIDERS, GenRequest  # noqa: E402
from vslib import storyboard as sb  # noqa: E402

EXPECTED_COMMANDS = {
    "doctor", "models", "setup", "init", "gen", "run", "list", "status",
    "fetch", "resume", "assemble", "plan", "clips", "estimate", "bible",
    "takes", "pick", "chain", "animatic", "qc",
    "voice", "align", "media", "edit", "export", "sheet", "cover", "presets",
}


class TestRegistration(unittest.TestCase):
    def test_all_commands_registered(self):
        import argparse
        ap = vs.build_parser()
        for action in ap._actions:
            if isinstance(action, argparse._SubParsersAction):
                self.assertTrue(EXPECTED_COMMANDS.issubset(set(action.choices)))
                return
        self.fail("没有找到子命令解析器")

    def test_gateways_registered(self):
        self.assertIn("fal", PROVIDERS)
        self.assertIn("heygen", PROVIDERS)
        self.assertEqual(Config.KEY_ENVS["fal"], "FAL_KEY")
        self.assertEqual(Config.KEY_ENVS["heygen"], "HEYGEN_API_KEY")

    def test_rate_snapshot_has_date(self):
        self.assertRegex(vs.RATES_AS_OF, r"\d{4}-\d{2}-\d{2}")


class TestDefaultsStayOff(unittest.TestCase):
    def test_post_plan_all_off(self):
        ap = vs.build_parser()
        args = ap.parse_args(["plan"])
        s = sb.Storyboard()
        plan = vs.resolve_post_plan(args, s)
        self.assertFalse(plan["subtitles"])
        self.assertFalse(plan["audio"])
        self.assertFalse(plan["fade"])
        self.assertEqual(plan["bgm"], "")
        self.assertEqual(plan["transition"], "none")
        self.assertEqual(plan["title"], "")

    def test_genrequest_audio_off_by_default(self):
        self.assertFalse(GenRequest(prompt="x").audio)
        args = vs.build_parser().parse_args(["gen", "-p", "x", "--audio"])
        self.assertTrue(vs.build_request(args).audio)

    def test_no_effects_in_storyboard_defaults(self):
        s = sb.Storyboard()
        self.assertFalse(s.audio)
        self.assertEqual(s.transition, "none")


class TestBudgetAndSetup(unittest.TestCase):
    def test_budget_guard_blocks(self):
        with self.assertRaises(SystemExit):
            vs.budget_guard(1.0, "ark", "doubao-seedance-1-0-pro-250528", 100)

    def test_budget_guard_allows(self):
        vs.budget_guard(1000.0, "ark", "doubao-seedance-1-0-pro-250528", 5)

    def test_setup_accepts_both_key_forms(self):
        seen = []
        with patch.object(Config, "write_env_line",
                          side_effect=lambda prov, val: seen.append((prov, val)) or Path(".env")):
            for pair in ("ARK_API_KEY=abc", "ark=def"):
                args = vs.build_parser().parse_args(["setup", "--set", pair])
                args.func(args)
        self.assertEqual([p for p, _ in seen], ["ark", "ark"])
        self.assertEqual([v for _, v in seen], ["abc", "def"])


if __name__ == "__main__":
    unittest.main()
