"""评测套件自身的正确性测试。

评测台如果不可信，用它得出的消融结论也不可信，所以这些测试是必需的。
"""
from __future__ import annotations

import csv
import math
import os
import re
import shutil
import tempfile
import unittest

from aml_retriever.config import DEFAULT_FLAGS
from aml_retriever.evaluation import metrics as M
from aml_retriever.evaluation.dataset import DIFFICULTIES, SCALES, SUITES, make_dataset
from aml_retriever.evaluation.harness import (
    ABLATION_LADDER, CONTROL_STAGE, MAINLINE_STAGES, PRODUCTION_STAGE,
    build_index, run_stage,
)
from aml_retriever.retriever import RetrieverDB
from aml_retriever.config import RetrieverConfig


class _FakeEvidence:
    def __init__(self, eid, sources=None):
        self.id = eid
        self.source_message_ids = list(sources or [])


class TestMetrics(unittest.TestCase):
    def test_recall_counts_via_source_messages(self):
        """聚合视图只要覆盖 gold 原始消息就算召回。"""
        results = [_FakeEvidence("w1", ["m1", "m2"]), _FakeEvidence("m9", ["m9"])]
        self.assertEqual(M.recall_at_k(results, {"m1", "m2"}, 20), 1.0)
        self.assertEqual(M.recall_at_k(results, {"m1", "m3"}, 20), 0.5)

    def test_recall_respects_k_cutoff(self):
        results = [_FakeEvidence(f"m{i}", [f"m{i}"]) for i in range(30)]
        self.assertEqual(M.recall_at_k(results, {"m25"}, 20), 0.0)
        self.assertEqual(M.recall_at_k(results, {"m25"}, 100), 1.0)

    def test_recall_is_nan_without_gold(self):
        self.assertTrue(math.isnan(M.recall_at_k([], set(), 20)))

    def test_reciprocal_rank(self):
        results = [_FakeEvidence("a", ["a"]), _FakeEvidence("b", ["b"]),
                   _FakeEvidence("c", ["c"])]
        self.assertEqual(M.reciprocal_rank(results, {"a"}), 1.0)
        self.assertAlmostEqual(M.reciprocal_rank(results, {"c"}), 1 / 3)
        self.assertEqual(M.reciprocal_rank(results, {"zzz"}), 0.0)

    def test_falls_back_to_result_id_without_provenance(self):
        self.assertEqual(M.recall_at_k([_FakeEvidence("m1")], {"m1"}, 10), 1.0)

    def test_percentile_interpolates(self):
        values = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(M.percentile(values, 0), 1.0)
        self.assertEqual(M.percentile(values, 100), 4.0)
        self.assertAlmostEqual(M.percentile(values, 50), 2.5)

    def test_latency_summary_shape(self):
        summary = M.summarize_latency([5.0, 1.0, 3.0])
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["max_ms"], 5.0)
        self.assertEqual(summary["p50_ms"], 3.0)

    def test_mean_ignores_nan(self):
        self.assertEqual(M.mean([1.0, float("nan"), 3.0]), 2.0)


class TestDataset(unittest.TestCase):
    def test_is_deterministic(self):
        a = make_dataset(seed=7, scale="smoke", difficulty="mixed")
        b = make_dataset(seed=7, scale="smoke", difficulty="mixed")
        self.assertEqual(a.dump(), b.dump())

    def test_seed_changes_content(self):
        a = make_dataset(seed=7, scale="smoke")
        b = make_dataset(seed=8, scale="smoke")
        self.assertNotEqual(a.dump()["sessions"], b.dump()["sessions"])

    def test_all_scales_and_difficulties_build(self):
        for scale in SCALES:
            for level in DIFFICULTIES:
                ds = make_dataset(seed=1, scale=scale, difficulty=level)
                self.assertGreater(ds.message_count, 0)
                self.assertGreater(len(ds.queries), 0)

    def test_rejects_unknown_scale_and_difficulty(self):
        with self.assertRaises(ValueError):
            make_dataset(scale="nope")
        with self.assertRaises(ValueError):
            make_dataset(difficulty="nope")
        with self.assertRaises(ValueError):
            make_dataset(suite="nope")

    def test_v11_suite_appends_probes_without_changing_classic(self):
        classic = make_dataset(seed=3, scale="smoke", suite="classic")
        v11 = make_dataset(seed=3, scale="smoke", suite="v11")
        self.assertEqual(classic.suite, "classic")
        self.assertEqual(v11.suite, "v11")
        self.assertEqual(len(v11.sessions), len(classic.sessions) + len(classic.users))
        self.assertEqual(len(v11.queries), len(classic.queries) + 2 * len(classic.users))
        kinds = v11.kind_counts()
        self.assertEqual(kinds["governance_update_noise"], len(v11.users))
        self.assertEqual(kinds["direct_preference"], len(v11.users))
        self.assertNotIn("governance_update_noise", classic.kind_counts())

    def test_gold_keys_point_at_real_slots(self):
        """每个 gold 键必须能在数据集中定位到一条真实消息。"""
        ds = make_dataset(seed=3, scale="smoke")
        index = {s["session_id"]: s["messages"] for s in ds.sessions}
        for query in ds.queries:
            for key in list(query.gold) + list(query.distractors):
                session_id, _, raw_idx = key.rpartition("#")
                self.assertIn(session_id, index, key)
                self.assertLess(int(raw_idx), len(index[session_id]), key)

    def test_absent_queries_have_no_gold(self):
        ds = make_dataset(seed=3, scale="smoke")
        absent = [q for q in ds.queries if q.kind == "absent"]
        self.assertTrue(absent)
        self.assertTrue(all(not q.gold for q in absent))

    def test_paraphrase_queries_avoid_gold_keyword(self):
        """改写档查询不得直接抄 gold 的关键属性词，否则基准会退化成词面匹配。"""
        ds = make_dataset(seed=3, scale="smoke", difficulty="paraphrase")
        single = [q for q in ds.queries if q.kind == "single_hop"]
        self.assertTrue(single)
        self.assertTrue(all("工位编号" not in q.text for q in single))

    def test_hard_distractors_share_the_protagonist(self):
        """硬干扰项与 gold 同主角，确保人名无法单独消歧。"""
        ds = make_dataset(seed=3, scale="smoke")
        session = ds.sessions[0]
        gold_query = next(q for q in ds.queries
                          if q.user_id == session["user_id"] and q.kind == "single_hop")
        hero = gold_query.text[:2]
        same_hero = [m for m in session["messages"]
                     if hero in m["content"] and "工位编号" not in m["content"]]
        self.assertGreaterEqual(len(same_hero), 3, "同主角干扰项过少，基准过于容易")

    def test_timestamps_are_monotonic_within_session(self):
        ds = make_dataset(seed=3, scale="smoke")
        for session in ds.sessions:
            stamps = [m["timestamp"] for m in session["messages"]]
            self.assertEqual(stamps, sorted(stamps))


class TestHarness(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="aml-eval-test-")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_build_index_maps_every_gold_key(self):
        ds = make_dataset(seed=5, scale="smoke")
        cfg = RetrieverConfig(db_path=os.path.join(self.workdir, "m.db"))
        db = RetrieverDB(cfg)
        try:
            index, id_map = build_index(db, ds)
            self.assertEqual(index["messages"], ds.message_count)
            for query in ds.queries:
                for key in list(query.gold) + list(query.distractors):
                    self.assertIn(key, id_map, f"gold 键 {key} 未映射到引擎消息 id")
            # 映射目标必须是引擎里真实存在的消息
            sample = next(iter(id_map.values()))
            rows = db.query("SELECT id FROM messages WHERE id=?", (sample,))
            self.assertTrue(rows)
        finally:
            db.close()

    def test_build_index_does_not_leak_local_db_path(self):
        """index.rows 里不得带本机 db_path：临时库跑完即删，路径落进产物只是噪声。"""
        ds = make_dataset(seed=5, scale="smoke")
        cfg = RetrieverConfig(db_path=os.path.join(self.workdir, "m.db"))
        db = RetrieverDB(cfg)
        try:
            index, _ = build_index(db, ds)
            self.assertNotIn("db_path", index["rows"])
            self.assertIn("messages", index["rows"])  # 行数统计必须保留
        finally:
            db.close()

    def test_run_stage_produces_metrics(self):
        ds = make_dataset(seed=5, scale="smoke")
        result = run_stage("t", dict(DEFAULT_FLAGS), ds, workdir=self.workdir, top_k=100)
        self.assertFalse(result.skipped)
        self.assertEqual(result.overall["queries"], len(ds.scored_queries))
        self.assertGreater(result.overall["recall@100"], 0.0)
        self.assertIn("p95_ms", result.latency)
        self.assertIn("single_hop", result.by_kind)

    def test_absent_queries_excluded_from_scoring(self):
        ds = make_dataset(seed=5, scale="smoke")
        result = run_stage("t", dict(DEFAULT_FLAGS), ds, workdir=self.workdir, top_k=100)
        self.assertEqual(result.overall["queries"], len(ds.queries) - len(
            [q for q in ds.queries if q.kind == "absent"]))
        self.assertIn("note", result.by_kind["absent"])

    def test_vector_stage_skips_without_backend(self):
        ds = make_dataset(seed=5, scale="smoke")
        flags = {**DEFAULT_FLAGS, "vector": True}
        result = run_stage("v", flags, ds, workdir=self.workdir, top_k=20)
        if result.skipped:
            self.assertIn("vector backend unavailable", result.skip_reason)
        else:  # 环境恰好装了 numpy 等依赖时，至少要跑出指标
            self.assertIn("recall@100", result.overall)

    def test_run_stage_is_reproducible(self):
        ds = make_dataset(seed=5, scale="smoke")
        first = run_stage("r", dict(DEFAULT_FLAGS), ds, workdir=self.workdir, top_k=50)
        second = run_stage("r", dict(DEFAULT_FLAGS), ds, workdir=self.workdir, top_k=50)
        self.assertEqual(first.overall, second.overall)
        self.assertEqual(first.by_kind, second.by_kind)


class TestLadderIntegrity(unittest.TestCase):
    def test_stage_names_are_unique(self):
        names = [name for name, _ in ABLATION_LADDER]
        self.assertEqual(len(names), len(set(names)))

    def test_flags_only_use_known_keys(self):
        for name, flags in ABLATION_LADDER:
            unknown = set(flags) - set(DEFAULT_FLAGS)
            self.assertFalse(unknown, f"{name} 含未知开关 {unknown}")

    def test_mainline_is_monotonically_cumulative(self):
        """主线每一级只允许新增开关，不允许关掉上一级已开的。"""
        table = dict(ABLATION_LADDER)
        previous: set[str] = set()
        for name in MAINLINE_STAGES:
            self.assertIn(name, table, f"主线档位 {name} 不在梯度表里")
            enabled = {k for k, v in table[name].items() if v}
            self.assertTrue(previous <= enabled, f"{name} 关掉了上一级的开关")
            previous = enabled

    def test_control_stages_have_isolated_deltas(self):
        """每个实验档必须相对其直接基线只改变声明的开关。"""
        table = dict(ABLATION_LADDER)
        legacy = {k for k, v in table["L5_plus_weighted_rrf"].items() if v}
        for name in ("L6_temporal_intent_ctrl", "L7_plus_vector",
                     "L8_supersession_ctrl"):
            enabled = {k for k, v in table[name].items() if v}
            self.assertEqual(len(enabled - legacy), 1, f"{name} 相对 L5 改动了多个开关")
            self.assertFalse(legacy - enabled, f"{name} 关掉了 L5 已开的开关")

        production = {k for k, v in table[PRODUCTION_STAGE].items() if v}
        preference = {k for k, v in table["L10_preference_ctrl"].items() if v}
        self.assertEqual(preference - production, {"preference_role_boost"})
        self.assertFalse(production - preference)
        guarded = {k for k, v in table["L9_guarded_supersession"].items() if v}
        unguarded = {k for k, v in table["L8_supersession_ctrl"].items() if v}
        self.assertEqual(guarded - unguarded, {"supersession_update_guard"})
        self.assertFalse(unguarded - guarded)

    def test_production_stage_matches_default_flags(self):
        """代码默认配置必须真的对应梯度里被标注的那一档，否则报告会误导人。"""
        stage = dict(dict(ABLATION_LADDER)[PRODUCTION_STAGE])
        for key, value in DEFAULT_FLAGS.items():
            self.assertEqual(bool(stage.get(key, False)), bool(value),
                             f"开关 {key} 与 DEFAULT_FLAGS 不一致")

    def test_control_stage_is_not_production(self):
        """CONTROL_STAGE 必须与 PRODUCTION_STAGE 是两个不同的档位。"""
        self.assertNotEqual(CONTROL_STAGE, PRODUCTION_STAGE)
        self.assertIn(CONTROL_STAGE, dict(ABLATION_LADDER))


def _load_run_eval():
    """把 scripts/run_eval.py 作为模块加载（scripts/ 不是包）。"""
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "scripts", "run_eval.py")
    spec = importlib.util.spec_from_file_location("_run_eval_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage_payload(name: str, mrr: float, recall20: float) -> dict:
    """构造一个形状合法的档位结果，数值仅用于断言渲染逻辑。"""
    return {
        "stage": name,
        "skipped": False,
        "overall": {"recall@20": recall20, "recall@100": 1.0, "mrr": mrr,
                    "distractor_leak@10": 0.0},
        "latency": {"p50_ms": 1.0, "p95_ms": 2.0},
        "index": {"elapsed_s": 0.5},
        "by_difficulty": {"mixed": {"mrr": mrr}},
        "by_kind": {"plain": {"queries": 10, "recall@20": recall20,
                              "recall@100": 1.0, "mrr": mrr,
                              "distractor_leak@10": 0.0}},
    }


def _fake_payload(stages: list[dict]) -> dict:
    return {
        "generated_at": "2026-08-07T00:00:00+00:00",
        "repo": "/tmp/repo",
        "top_k": 100,
        "dataset": {"scale": "medium", "difficulty": "mixed", "seed": 20260806,
                    "users": 1, "sessions": 1, "messages": 1, "queries": 10,
                    "scored_queries": 10, "queries_by_kind": {},
                    "queries_by_difficulty": {}},
        "environment": {"python": "3.13", "sqlite_library_version": "3.50",
                        "fts5": True, "vector_backend_available": False,
                        "vector_backend_reason": "no deps"},
        "stages": stages,
    }


class TestReportStageRoles(unittest.TestCase):
    """报告 section 3 必须锁定声明的生产档与直接对照档。

    历史缺陷：section 3 用 reversed(stages) 取"最后一个未跳过档位"，
    会让后置实验档冒充代码默认。
    """

    def setUp(self):
        self.mod = _load_run_eval()
        self.dir = tempfile.mkdtemp(prefix="aml-report-")
        self.path = os.path.join(self.dir, "REPORT.md")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _render(self, stages) -> str:
        self.mod.write_report(self.path, _fake_payload(stages))
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def _section3(self, text: str) -> str:
        body = text.split("## 3.")[1]
        return body.split("## 4.")[0]

    def test_section3_picks_production_not_last_stage(self):
        """即使输入顺序把对照档放最后，也必须展示声明的生产档。"""
        text = self._render([
            _stage_payload(PRODUCTION_STAGE, 0.6631, 1.0),
            _stage_payload(CONTROL_STAGE, 0.4000, 0.9),
        ])
        section = self._section3(text)
        self.assertIn(f"档位：`{PRODUCTION_STAGE}`", section)
        self.assertIn("代码默认", section)
        self.assertNotIn(f"档位：`{CONTROL_STAGE}`", section)

    def test_section3_marks_control_role_explicitly(self):
        """section 3 必须写明直接基线是对照组且增强默认关闭。"""
        section = self._section3(self._render([
            _stage_payload(PRODUCTION_STAGE, 0.6631, 1.0),
            _stage_payload(CONTROL_STAGE, 0.4000, 0.9),
        ]))
        self.assertIn(CONTROL_STAGE, section)
        self.assertIn("对照组", section)
        self.assertIn("关闭", section)

    def test_production_marked_in_overview_table(self):
        text = self._render([
            _stage_payload(PRODUCTION_STAGE, 0.6631, 1.0),
            _stage_payload(CONTROL_STAGE, 0.4000, 0.9),
        ])
        overview = text.split("## 1.")[1].split("## 2.")[0]
        production_row = [l for l in overview.splitlines()
                          if f"`{PRODUCTION_STAGE}`" in l][0]
        control_row = [l for l in overview.splitlines()
                       if f"`{CONTROL_STAGE}`" in l][0]
        self.assertIn("代码默认", production_row)
        self.assertNotIn("代码默认", control_row)

    def test_skipped_production_falls_back_with_warning(self):
        """代码默认档位未运行时必须显式告警，不得静默冒充。"""
        skipped = _stage_payload(PRODUCTION_STAGE, 0.0, 0.0)
        skipped["skipped"] = True
        section = self._section3(self._render([
            _stage_payload("L0_lexical_baseline", 0.5, 0.8),
            skipped,
        ]))
        self.assertIn("L0_lexical_baseline", section)
        self.assertIn("不代表代码默认", section)


def _agg(mean: float, lo: float, hi: float, n: int = 3) -> dict:
    return {"mean": mean, "min": lo, "max": hi, "n": n}


def _multiseed_payload(difficulty: str, cross_cells: dict) -> dict:
    """构造一个形状合法的 multiseed payload；cross_cells 决定交叉表里有哪些格。"""
    def stage(name):
        return {
            "stage": name,
            "flags": {},
            "skipped": False,
            "overall": {"recall@20": _agg(1.0, 1.0, 1.0),
                        "recall@100": _agg(1.0, 1.0, 1.0),
                        "mrr": _agg(0.7, 0.6, 0.8),
                        "distractor_leak@10": _agg(0.1, 0.0, 0.2)},
            "latency": {"p50_ms": _agg(9.0, 8.0, 10.0),
                        "p95_ms": _agg(17.0, 15.0, 19.0)},
            "by_kind_difficulty": {
                ck: {"mrr": _agg(*vals), "recall@20": _agg(1.0, 1.0, 1.0),
                     "distractor_leak@10": _agg(0.0, 0.0, 0.0)}
                for ck, vals in cross_cells.items()
            },
        }

    def per_seed_row(seed, name):
        return {"seed": seed, "stage": name, "skipped": False,
                "overall": {"recall@20": 1.0, "recall@100": 1.0, "mrr": 0.7,
                            "distractor_leak@10": 0.1},
                "latency": {"p50_ms": 9.0, "p95_ms": 17.0}}

    names = [PRODUCTION_STAGE, CONTROL_STAGE, "L8_supersession_ctrl"]
    seeds = [20260806, 20260807, 20260808]
    return {
        "generated_at": "2026-08-07T00:00:00+0800",
        "repo": "/tmp/repo",
        "mode": "multiseed",
        "seeds": seeds,
        "scale": "medium",
        "difficulty": difficulty,
        "top_k": 100,
        "environment": {"python": "3.13", "sqlite_library_version": "3.50", "fts5": True},
        "per_seed": [[per_seed_row(s, n) for n in names] for s in seeds],
        "aggregate": [stage(n) for n in names],
    }


class TestMultiseedReport(unittest.TestCase):
    """跨 seed 报告必须完整暴露 p50/p95，并在交叉格缺失时显式说明而非静默留白。"""

    def setUp(self):
        self.mod = _load_run_eval()
        self.dir = tempfile.mkdtemp(prefix="aml-multiseed-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _render(self, payload) -> str:
        path = os.path.join(self.dir, "R.md")
        self.mod.write_multiseed_report(path, payload)
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_overview_and_per_seed_tables_include_latency(self):
        """任务要求逐 seed 与聚合都给出 p50/p95，缺列即视为产物不合格。"""
        text = self._render(_multiseed_payload("mixed", {"temporal|paraphrase": (0.6, 0.5, 0.7)}))
        overview = text.split("## 1.")[1].split("## 2.")[0]
        self.assertIn("p50 (ms)", overview)
        self.assertIn("p95 (ms)", overview)
        per_seed = text.split("## 3.")[1].split("## 4.")[0]
        for col in ("Recall@20", "Recall@100", "MRR", "旧值泄漏@10", "p50 (ms)", "p95 (ms)"):
            self.assertIn(col, per_seed)
        self.assertIn("20260808", per_seed)

    def test_focus_section_uses_paraphrase_cell_when_present(self):
        text = self._render(_multiseed_payload("mixed", {"temporal|paraphrase": (0.6, 0.5, 0.7)}))
        section = text.split("## 2.")[1].split("## 3.")[0]
        self.assertIn("temporal|paraphrase", section)
        self.assertNotIn("（无该交叉格）", section)

    def test_focus_section_falls_back_and_warns_when_paraphrase_absent(self):
        """difficulty=plain 时不存在 paraphrase 格，必须回退并明确警告不可据此判断短板。"""
        text = self._render(_multiseed_payload("plain", {"temporal|plain": (0.9, 0.9, 1.0)}))
        section = text.split("## 2.")[1].split("## 3.")[0]
        self.assertIn("temporal|plain", section)
        self.assertIn("不能", section)
        self.assertNotIn("（无该交叉格）", section)

    def test_artifacts_roundtrip_without_rerunning_eval(self):
        """--from-json 重渲染必须产出四件套，且逐 seed CSV 每 (seed,档位) 一行。"""
        payload = _multiseed_payload("mixed", {"temporal|paraphrase": (0.6, 0.5, 0.7)})
        written = self.mod.write_multiseed_artifacts(self.dir, payload)
        self.assertEqual(len(written), 4)
        for p in written:
            self.assertTrue(os.path.exists(p), p)
        per_seed_csv = [p for p in written if p.endswith("_per_seed.csv")][0]
        with open(per_seed_csv, encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0][:3], ["seed", "stage", "skipped"])
        self.assertIn("p95_ms", rows[0])
        self.assertEqual(len(rows) - 1, 3 * 3)  # 3 seeds × 3 档位

    def test_artifacts_contain_no_corpus_text(self):
        """产物只允许出现指标数字与元数据，不得夹带语料原文或本机临时路径。

        注意：``index.messages`` 是**计数**不是原文，不能简单按 key 名封杀；
        这里改为检查是否出现成句的中文正文（长 CJK 串）以及临时目录路径。
        """
        payload = _multiseed_payload("mixed", {"temporal|paraphrase": (0.6, 0.5, 0.7)})
        cjk_sentence = re.compile(r"[\u4e00-\u9fff]{12,}")
        for p in self.mod.write_multiseed_artifacts(self.dir, payload):
            with open(p, encoding="utf-8") as fh:
                body = fh.read()
            if p.endswith(".md"):
                continue  # 报告正文本就是中文说明，只约束数据产物
            self.assertIsNone(cjk_sentence.search(body),
                              f"{os.path.basename(p)} 疑似夹带语料原文")
            self.assertNotIn("/T/aml-eval-", body,
                             f"{os.path.basename(p)} 泄漏了本机临时库路径")
            self.assertNotIn("/tmp/repo", body,
                             f"{os.path.basename(p)} 泄漏了 checkout 路径")


if __name__ == "__main__":
    unittest.main()
