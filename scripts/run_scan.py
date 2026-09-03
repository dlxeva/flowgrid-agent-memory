#!/usr/bin/env python3
"""AML Retriever 参数扫描（为 DEFAULT_FLAGS 的取值提供离线证据）。

与 ``run_eval.py`` 的区别：
  - ``run_eval.py`` 跑**功能开关的累进消融**，每档重建索引；
  - ``run_scan.py`` 跑**同一索引下的参数取值扫描**，只改检索期权重。

之所以能复用索引：写入期唯一依赖的开关是 ``views``（retriever.py:363），
本脚本全程固定 ``views=True``，因此索引对所有扫描点完全一致——
这让扫描点之间的差异**只**来自被扫描的那个参数，而不是索引噪声。

产物：
  eval_out/scan_<name>_<scale>_<seed>.json / .csv

全程纯合成数据、零第三方依赖、不联网。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aml_retriever.config import RetrieverConfig, DEFAULT_FLAGS          # noqa: E402
from aml_retriever.evaluation import make_dataset, SCALES, SUITES        # noqa: E402
from aml_retriever.evaluation.dataset import DIFFICULTIES                # noqa: E402
from aml_retriever.evaluation import metrics as M                        # noqa: E402
from aml_retriever.evaluation.harness import (                           # noqa: E402
    OFFICIAL_TOP_K, build_index,
)
from aml_retriever.retriever import RetrieverDB                          # noqa: E402

CSV_COLUMNS = [
    "scan", "suite", "difficulty", "point", "queries",
    "recall@20", "recall@100", "mrr", "distractor_leak@10",
    "mrr_temporal", "recall@20_temporal",
    "mrr_knowledge_update", "recall@20_knowledge_update",
    "mrr_multi_session", "recall@20_multi_session",
    "mrr_governance_update_noise", "recall@20_governance_update_noise",
    "mrr_direct_preference", "recall@20_direct_preference",
    "p50_ms", "p95_ms",
]


# --------------------------------------------------------------------- 扫描定义
def rrf_points() -> list[tuple[str, dict]]:
    """附录 A：加权 RRF 的词法权重扫描。

    对照点 ``rrf_off`` = DEFAULT_FLAGS 关掉 rrf（即消融梯度里的 L4）。
    其余点开启 rrf，只改 ``rrf_weight_lexical``。
    """
    points: list[tuple[str, dict]] = [("rrf_off", {"flags": {"rrf": False}})]
    for w in (0.1, 0.25, 0.5, 1.0):
        points.append((f"rrf_w_lex={w}", {"flags": {"rrf": True},
                                          "config": {"rrf_weight_lexical": w}}))
    return points


def temporal_points() -> list[tuple[str, dict]]:
    """附录 B：时间意图放大（temporal_intent）开关扫描。"""
    return [
        ("temporal_intent_off", {"flags": {"temporal_intent": False}}),
        ("temporal_intent_on", {"flags": {"temporal_intent": True}}),
    ]


def supersession_points() -> list[tuple[str, dict]]:
    """v1.1：显式更新保护与成对覆写权重扫描。"""
    points: list[tuple[str, dict]] = [
        ("supersession_off", {
            "flags": {"supersession": False, "supersession_update_guard": False},
        }),
        ("supersession_unguarded", {
            "flags": {"supersession": True, "supersession_update_guard": False},
        }),
    ]
    for weight, penalty in ((4.0, 1.0), (6.0, 2.0), (8.0, 2.0), (10.0, 3.0),
                            (12.0, 4.0), (14.0, 4.0), (18.0, 6.0)):
        points.append((
            f"guarded_w={weight:g}_p={penalty:g}",
            {
                "flags": {"supersession": True, "supersession_update_guard": True},
                "config": {
                    "supersession_weight": weight,
                    "supersession_penalty": penalty,
                },
            },
        ))
    return points


SCANS = {
    "rrf": ("加权 RRF 词法权重扫描", rrf_points),
    "temporal": ("时间意图放大开关扫描", temporal_points),
    "supersession": ("显式更新保护与覆写权重扫描", supersession_points),
}


# --------------------------------------------------------------------- 执行
def _round(value, digits: int = 4):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(float(value), digits)


def evaluate(db: RetrieverDB, dataset, id_map: dict, top_k: int) -> dict:
    """在当前 db.flags / db.config 下跑完整查询集。"""
    latencies: list[float] = []
    rows: list[dict] = []
    for query in dataset.queries:
        gold = {id_map[g] for g in query.gold if g in id_map}
        distractors = {id_map[d] for d in query.distractors if d in id_map}
        started = time.perf_counter()
        result = db.search(user_id=query.user_id, query=query.text, top_k=top_k)
        latencies.append((time.perf_counter() - started) * 1000.0)
        hits = result.results
        rows.append({
            "kind": query.kind,
            "recall@20": M.recall_at_k(hits, gold, 20),
            "recall@100": M.recall_at_k(hits, gold, 100),
            "mrr": M.reciprocal_rank(hits, gold),
            "leak@10": M.distractor_leak_at_k(hits, distractors, 10),
        })

    scored = [r for r in rows if not math.isnan(r["recall@100"])]
    by_kind = {
        kind: [r for r in scored if r["kind"] == kind]
        for kind in (
            "temporal", "knowledge_update", "multi_session",
            "governance_update_noise", "direct_preference",
        )
    }
    latency = M.summarize_latency(latencies)
    result = {
        "queries": len(scored),
        "recall@20": _round(M.mean([r["recall@20"] for r in scored])),
        "recall@100": _round(M.mean([r["recall@100"] for r in scored])),
        "mrr": _round(M.mean([r["mrr"] for r in scored])),
        "distractor_leak@10": _round(M.mean([r["leak@10"] for r in scored])),
        "p50_ms": latency.get("p50_ms"),
        "p95_ms": latency.get("p95_ms"),
    }
    for kind, subset in by_kind.items():
        result[f"mrr_{kind}"] = _round(M.mean([r["mrr"] for r in subset])) if subset else None
        result[f"recall@20_{kind}"] = (
            _round(M.mean([r["recall@20"] for r in subset])) if subset else None
        )
    return result


def run_scan(scan: str, *, scale: str, seed: int, difficulties: list[str],
             suite: str, top_k: int, quiet: bool = False) -> dict:
    label, factory = SCANS[scan]
    points = factory()
    results: list[dict] = []

    workdir = tempfile.mkdtemp(prefix=f"aml-scan-{scan}-")
    try:
        for difficulty in difficulties:
            dataset = make_dataset(
                seed=seed, scale=scale, difficulty=difficulty, suite=suite
            )
            db_path = os.path.join(workdir, f"{difficulty}.db")
            # 索引期配置固定为生产默认（views=True），保证各扫描点共享同一索引
            config = RetrieverConfig(db_path=db_path, top_k_default=top_k,
                                     top_k_max=max(top_k, 100))
            config.flags = dict(DEFAULT_FLAGS)
            config.flags["views"] = True
            db = RetrieverDB(config)
            try:
                index, id_map = build_index(db, dataset)
                if not quiet:
                    print(f"[scan] {scan}/{difficulty} indexed "
                          f"{index['messages']} msgs in {index['elapsed_s']}s")
                for name, override in points:
                    # 每个扫描点都从生产默认重新起算，只改被扫描的量
                    db.flags = dict(DEFAULT_FLAGS)
                    db.flags["views"] = True
                    for key, value in (override.get("flags") or {}).items():
                        db.flags[key] = value
                    fresh = RetrieverConfig()
                    for attr in ("rrf_k", "rrf_weight_feature", "rrf_weight_lexical",
                                 "recency_weight", "recency_weight_intent",
                                 "supersession_weight", "supersession_penalty"):
                        setattr(db.config, attr, getattr(fresh, attr))
                    for key, value in (override.get("config") or {}).items():
                        setattr(db.config, key, value)

                    metrics = evaluate(db, dataset, id_map, top_k)
                    row = {"scan": scan, "suite": suite, "difficulty": difficulty,
                           "point": name, **metrics}
                    results.append(row)
                    if not quiet:
                        print(f"[scan] {scan:9s} {difficulty:11s} {name:20s} "
                              f"MRR={row['mrr']} R@20={row['recall@20']} "
                              f"p95={row['p95_ms']}ms")
            finally:
                db.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return {
        "scan": scan,
        "label": label,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "scale": scale,
        "suite": suite,
        "seed": seed,
        "top_k": top_k,
        "difficulties": difficulties,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "sqlite_library_version": sqlite3.sqlite_version,
            "third_party_packages_installed": False,
            "network_access_used": False,
        },
        "index_shared_across_points": True,
        "rows": results,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AML Retriever 参数扫描")
    parser.add_argument("--scan", default="rrf", choices=sorted(SCANS) + ["all"])
    parser.add_argument("--scale", default="medium", choices=sorted(SCALES))
    parser.add_argument("--seed", type=int, default=20260806)
    parser.add_argument("--suite", default="classic", choices=list(SUITES))
    parser.add_argument("--difficulties", default="plain,paraphrase,mixed")
    parser.add_argument("--top-k", type=int, default=OFFICIAL_TOP_K)
    parser.add_argument("--out", default="eval_out")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = args.out if os.path.isabs(args.out) else os.path.join(repo, args.out)
    os.makedirs(out_dir, exist_ok=True)

    difficulties = [d.strip() for d in args.difficulties.split(",") if d.strip()]
    for d in difficulties:
        if d not in DIFFICULTIES:
            parser.error(f"unknown difficulty: {d}")

    wanted = sorted(SCANS) if args.scan == "all" else [args.scan]
    for scan in wanted:
        payload = run_scan(scan, scale=args.scale, seed=args.seed,
                           difficulties=difficulties, suite=args.suite,
                           top_k=args.top_k, quiet=args.quiet)
        suite_tag = "" if args.suite == "classic" else f"_{args.suite}"
        tag = f"{scan}_{args.scale}{suite_tag}_{args.seed}"
        json_path = os.path.join(out_dir, f"scan_{tag}.json")
        csv_path = os.path.join(out_dir, f"scan_{tag}.csv")
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        with open(csv_path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, lineterminator="\n")
            writer.writeheader()
            for row in payload["rows"]:
                writer.writerow({k: row.get(k) for k in CSV_COLUMNS})
        if not args.quiet:
            print(f"[scan] wrote {json_path}")
            print(f"[scan] wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
