import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lib
import pick_entries


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def playback(open_ms=7648, resume_ms=2, **overrides):
    return {"ran": True, "ok": True, "openMillis": open_ms,
            "timeToPlayingMillis": resume_ms, **overrides}


class EvalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        save(self.root / "meta.json", {
            "evalDate": "2026-01-01", "sourcesRoot": "configs", "egress": "test",
            "subjects": [{"subjectId": 1, "episodeId": 11, "name": "测试番"}],
        })
        save(self.root / "configs/t3/测试源.json", {"clientVersions": ">=6.3.0"})

    def report(self):
        spec = importlib.util.spec_from_file_location("eval_report_test", SCRIPTS / "gen_report.py")
        module = importlib.util.module_from_spec(spec)
        with patch.object(sys, "argv", ["gen_report.py", str(self.root)]):
            spec.loader.exec_module(module)
        return module

    def candidate(self, name="测试番", url="https://example.test/1"):
        return {"name": name, "fullUrl": url, "partialUrl": "/1", "keyword": "测试番", "order": 0}

    def test_startup_requires_success_and_both_measurements(self):
        self.assertEqual(lib.startup_millis(playback()), 7650)
        self.assertEqual(lib.startup_millis(playback(0, 0)), 0)
        for value in [None, {}, playback(None), playback(resume_ms=None),
                      playback(ok=False), playback(ran=False)]:
            with self.subTest(value=value):
                self.assertIsNone(lib.startup_millis(value))

    def test_title_matching_keeps_sequels_out(self):
        picked = pick_entries.auto_pick(["测试番"], [
            self.candidate("测试番 第二季", "https://example.test/2"),
            self.candidate(), self.candidate(),
        ])
        self.assertEqual(picked["urls"], ["https://example.test/1"])
        self.assertEqual(pick_entries.auto_pick(["测试番"], [self.candidate("测试番 剧场版")])["status"],
                         "needs_review")
        self.assertEqual(lib.normalize_title("测试番 第二季！"), lib.normalize_title("测试番 Season 2"))

    def test_check_rejects_missing_and_incomplete_picks(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pick_entries.print_status(self.root), 1)
        for rec in [{"status": "needs_review"}, {"status": "agent", "urls": []}, {"status": "unknown"}]:
            with self.subTest(rec=rec):
                save(self.root / lib.PICKS_REL, {"sources": {"测试源": {"1": rec}}})
                with self.assertRaises(SystemExit):
                    lib.require_picks(self.root, ["测试源"], [1])
        save(self.root / lib.PICKS_REL, {"sources": {"测试源": {"1": {"status": "unlisted"}}}})
        self.assertIsNotNone(lib.require_picks(self.root, ["测试源"], [1]))

    def test_refresh_preserves_reviewed_decisions(self):
        candidate = self.candidate()

        class FakeMcp:
            def __init__(self, *args):
                pass

            def start(self):
                pass

            def stop(self):
                pass

            def call(self, name, args, timeout):
                return {"subjects": [candidate], "steps": [
                    {"name": "searchSubjects", "status": "success"},
                ]}

        for rec in [{"status": "agent", "url": "https://example.test/reviewed", "note": "已确认"},
                    {"status": "unlisted", "note": "只有续季"}]:
            with self.subTest(rec=rec):
                save(self.root / lib.PICKS_REL, {"sources": {"测试源": {"1": rec}}})
                with patch.object(pick_entries, "Mcp", FakeMcp), \
                     patch.object(pick_entries, "require_mcp_bin", return_value=Path("unused")), \
                     contextlib.redirect_stdout(io.StringIO()):
                    pick_entries.discover(self.root, [], refresh=True)
                actual = lib.load_picks(self.root)["sources"]["测试源"]["1"]
                self.assertEqual(actual, rec)

    def write_played_report(self, pb):
        subject = self.root / "subjects/1-测试番"
        save(subject / "summary.json", [{
            "source": "测试源", "tier": "t3", "resolveOk": True,
            "perChannel": [{"channel": "线路1", "playerOk": True, "resolution": "1920x1080",
                            "bitrate": 1800000, "codec": "h264", "startupMillis": 2,
                            "timeToPlayingMillis": 2}],
        }])
        save(subject / "sources/t3-测试源.json", {
            "resolve": {}, "playerProbes": [{"channel": "线路1", "probe": {
                "mediaAnalysis": {"available": True, "playback": pb},
            }}],
        })
        save(self.root / "combined/frames_verdicts.json", {
            "_channel_level": {"测试源/线路1": {"ad": "none", "note": "fixture"}},
        })

    def test_report_recomputes_old_traces_and_does_not_double_count_summary(self):
        self.write_played_report(playback())
        save(self.root / "deep/t3-测试源/deep.json", {
            "source": "测试源", "tier": "t3", "runs": [{"subjectId": "1", "subjectName": "测试番",
                "channels": [{"channel": "线路1", "probe": {"mediaAnalysis": {
                    "playback": playback(8048), "video": {"width": 1920, "height": 1080, "codec": "h264"},
                }}, "ffprobe": {"streams": {"width": 1920, "height": 1080},
                                 "measured": {"avgBitrate": 2200000}}}],
            }],
        })
        report = self.report()
        subjects = report.load_subjects()
        agg = report.aggregate(subjects, report.load_deep())["测试源"]
        stats = report.channel_stats("测试源", "线路1", agg["channels"]["线路1"], 1)
        self.assertEqual(stats["ttp"], 7850)
        self.assertEqual(stats["br"], 2000000)
        self.assertEqual((stats["br_p10"], stats["br_p90"]), (1840000, 2160000))
        self.assertEqual(report.capability_tier(stats), "T3")

    def test_missing_open_time_does_not_qualify_for_fast_tiers(self):
        self.write_played_report(playback(None))
        report = self.report()
        agg = report.aggregate(report.load_subjects(), {})["测试源"]
        stats = report.channel_stats("测试源", "线路1", agg["channels"]["线路1"], 1)
        self.assertIsNone(stats["ttp"])
        self.assertEqual(report.capability_tier(stats), "T5")

    def test_t0_requires_stable_playback_on_enough_subjects(self):
        report = self.report()

        def tier(ok, appear, ignore_stability=False, **overrides):
            return report.capability_tier({"ad_rank": 0, "res": "1920x1080", "br": 2500000, "ttp": 1000,
                                           "ok_n": ok, "appear": appear, **overrides}, ignore_stability)

        self.assertEqual(tier(5, 5), "T0")
        self.assertEqual(tier(9, 9), "T0")
        self.assertEqual(tier(4, 4), "T1")   # 全部可播但样本不足 5 部
        self.assertEqual(tier(9, 10), "T1")  # 有失败
        self.assertEqual(tier(1, 5), "T1")
        self.assertEqual(tier(3, 5, ignore_stability=True), "T0")
        self.assertEqual(tier(3, 5, br=1300000), "T2")  # 指标本来就不到 T1 的不受影响
        # 稳定性只压低, 不抬高: 指标只够 T3 的线路再稳定也是 T3
        self.assertEqual(tier(5, 5, br=1100000), "T3")
        self.assertEqual(tier(1, 5, br=1100000), "T3")

    def test_supplementary_subject_only_applies_to_listed_sources(self):
        extra = {"subjectId": 2, "episodeId": 22, "name": "补测番", "onlySources": ["测试源"]}
        self.assertTrue(lib.subject_applies(extra, "测试源"))
        self.assertFalse(lib.subject_applies(extra, "另一源"))
        self.assertTrue(lib.subject_applies({"subjectId": 1}, "另一源"))
        save(self.root / "meta.json", {
            "evalDate": "2026-01-01", "sourcesRoot": "configs", "egress": "test",
            "subjects": [{"subjectId": 1, "episodeId": 11, "name": "测试番"}, extra],
        })
        save(self.root / "configs/t3/另一源.json", {})
        ok = {"status": "auto", "url": "https://example.test/1"}
        save(self.root / lib.PICKS_REL, {"sources": {"测试源": {"1": ok, "2": ok}, "另一源": {"1": ok}}})
        applies = lambda src, sid: lib.subject_applies({1: {}, 2: extra}[sid], src)
        self.assertIsNotNone(lib.require_picks(self.root, ["测试源", "另一源"], [1, 2], applies=applies))
        with self.assertRaises(SystemExit):
            lib.require_picks(self.root, ["测试源", "另一源"], [1, 2])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pick_entries.print_status(self.root), 0)

    def test_report_keeps_supplementary_subjects_out_of_source_level_columns(self):
        def played(source, sid, name):
            subject = self.root / f"subjects/{sid}-{name}"
            rows = json.loads((subject / "summary.json").read_text(encoding="utf-8")) \
                if (subject / "summary.json").exists() else []
            rows.append({"source": source, "tier": "t3", "resolveOk": True,
                         "perChannel": [{"channel": "线路1", "resolution": "1920x1080", "bitrate": 2500000}]})
            save(subject / "summary.json", rows)
            save(subject / f"sources/t3-{source}.json", {"resolve": {}, "playerProbes": [{
                "channel": "线路1", "probe": {"mediaAnalysis": {"available": True, "playback": playback(900)}}}]})

        subjects = [{"subjectId": i, "episodeId": i, "name": f"番{i}"} for i in range(1, 4)]
        subjects += [{"subjectId": i, "episodeId": i, "name": f"补{i}", "onlySources": ["测试源"]} for i in (4, 5)]
        save(self.root / "meta.json", {"evalDate": "2026-01-01", "egress": "test", "subjects": subjects})
        for sub in subjects:
            played("测试源", sub["subjectId"], sub["name"])
            if "onlySources" not in sub:
                played("另一源", sub["subjectId"], sub["name"])
        save(self.root / "combined/frames_verdicts.json", {"_channel_level": {
            "测试源": {"ad": "none", "note": "fixture"}, "另一源": {"ad": "none", "note": "fixture"}}})
        report = self.report()
        with contextlib.redirect_stdout(io.StringIO()):
            report.main()
        readme = (self.root / "README.md").read_text(encoding="utf-8")
        self.assertIn("测试番剧: 番1, 番2, 番3\n", readme)
        self.assertIn("补测番剧 (只对 测试源 追加", readme)
        # 补测让 测试源 的线路凑够 5 部全部可播 → T0; 另一源 只有 3 部 → T1, 标出被稳定性压低
        self.assertIn("| **T0** | 1920x1080 | 2.50M | 2.50M–2.50M | - | 902ms | 无 | 5/5 | ✅ | ✅ | ✅ |", readme)
        self.assertIn("| **T1**↓ | 1920x1080 | 2.50M | 2.50M–2.50M | - | 902ms | 无 | 3/3 | ✅ | ✅ | ✅ |", readme)
        # 源级的收录/成功率只数基础番剧, 补测单列
        self.assertIn("| t3 | 3/3 | 3/3 | 2/2 | 线路1 |", readme)
        self.assertIn("| t3 | 3/3 | 3/3 | - | 线路1 |", readme)
        self.assertIn("| [测试源](sources/t3-测试源.md) | **线路1** | t3 | 1920x1080 | 2.50M | 902ms | 3/3 |", readme)
        source_page = (self.root / "sources/t3-测试源.md").read_text(encoding="utf-8")
        self.assertIn("| 补4 (补测) |", source_page)
        self.assertNotIn("补4", (self.root / "sources/t3-另一源.md").read_text(encoding="utf-8"))

    def test_unlisted_source_is_not_reported_as_failed(self):
        save(self.root / "subjects/1-测试番/summary.json", [{
            "source": "测试源", "tier": "t3", "unlisted": True, "perChannel": [],
            "entry": {"status": "unlisted"},
        }])
        report = self.report()
        with contextlib.redirect_stdout(io.StringIO()):
            report.main()
        readme = (self.root / "README.md").read_text(encoding="utf-8")
        self.assertIn("**0** 个全部失败", readme)
        self.assertIn("**1** 个测试番剧均未收录", readme)
        self.assertNotIn("| 测试源 | t3 | 收录 |", readme)


if __name__ == "__main__":
    unittest.main()
