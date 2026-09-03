#!/usr/bin/env python3
"""AML Retriever 离线消融评测入口。

用法::

    python3 scripts/run_eval.py --scale small
    python3 scripts/run_eval.py --scale medium --top-k 100 --out eval_out
    python3 scripts/run_eval.py --scale smoke --stages L0_lexical_baseline,L4_plus_rrf_dedup
    python3 scripts/run_eval.py --scale small --dump-dataset eval_out/dataset.json

产物：``<out>/ablation_<scale>_<seed>.json`` 与同名 ``.csv``，
外加人可读的 ``<out>/REPORT_<scale>.md``。
全程纯合成数据、零第三方依赖、不联网。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aml_retriever.config import vector_backend_available          # noqa: E402
from aml_retriever.evaluation import make_dataset, SCALES, SUITES  # noqa: E402
from aml_retriever.evaluation.dataset import DIFFICULTIES          # noqa: E402
from aml_retriever.evaluation.harness import (                     # noqa: E402
    ABLATION_LADDER, CONTROL_STAGE, OFFICIAL_TOP_K, PRODUCTION_STAGE, run_ladder,
    aggregate_across_seeds, run_ladder_seeds,
)

CSV_COLUMNS = [
    "stage", "skipped", "queries", "recall@20", "recall@100", "mrr",
    "distractor_leak@10", "avg_returned",
    "p50_ms", "p95_ms", "p99_ms", "max_ms", "index_elapsed_s", "messages_per_s",
]

PUBLIC_REPO = "flowgrid-aml-retriever"


def environment() -> dict:
    available, reason = vector_backend_available()
    return {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "sqlite_library_version": sqlite3.sqlite_version,
        "fts5": _fts5_available(),
        "vector_backend_available": available,
        "vector_backend_reason": reason,
        "third_party_packages_installed": False,
        "network_access_used": False,
    }


def _fts5_available() -> bool:
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


def to_csv_row(result) -> dict:
    overall = result.overall or {}
    latency = result.latency or {}
    index = result.index or {}
    return {
        "stage": result.stage,
        "skipped": int(bool(result.skipped)),
        "queries": overall.get("queries"),
        "recall@20": overall.get("recall@20"),
        "recall@100": overall.get("recall@100"),
        "mrr": overall.get("mrr"),
        "distractor_leak@10": overall.get("distractor_leak@10"),
        "avg_returned": overall.get("avg_returned"),
        "p50_ms": latency.get("p50_ms"),
        "p95_ms": latency.get("p95_ms"),
        "p99_ms": latency.get("p99_ms"),
        "max_ms": latency.get("max_ms"),
        "index_elapsed_s": index.get("elapsed_s"),
        "messages_per_s": index.get("messages_per_s"),
    }


def fmt(value, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def write_report(path: str, payload: dict) -> None:
    ds = payload["dataset"]
    lines = [
        "# AML Retriever 离线消融评测报告",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 数据集：**纯合成**，suite=`{ds.get('suite', 'classic')}`，"
        f"scale=`{ds['scale']}`，difficulty=`{ds['difficulty']}`，"
        f"seed=`{ds['seed']}`",
        f"- 规模：{ds['users']} users / {ds['sessions']} sessions / "
        f"{ds['messages']} messages / {ds['queries']} queries"
        f"（计分 {ds['scored_queries']} 条）",
        f"- 查询构成：{ds['queries_by_kind']}；难度分布：{ds['queries_by_difficulty']}",
        f"- top_k：{payload['top_k']}（官方正式评测口径）",
        f"- 运行环境：Python {payload['environment']['python']} / "
        f"SQLite {payload['environment']['sqlite_library_version']} / "
        f"FTS5={payload['environment']['fts5']}",
        f"- 向量后端：{'可用' if payload['environment']['vector_backend_available'] else '不可用'}"
        f"（{payload['environment']['vector_backend_reason']}）",
        "",
        "## 1. 消融梯度总览",
        "",
        "| 档位 | Recall@20 | Recall@100 | MRR | 旧值泄漏@10 | p50(ms) | p95(ms) | 建库(s) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for stage in payload["stages"]:
        mark = " **(v1.1 代码默认)**" if stage["stage"] == PRODUCTION_STAGE else ""
        if stage["skipped"]:
            lines.append(f"| `{stage['stage']}`{mark} | 跳过 | 跳过 | 跳过 | 跳过 | — | — | — |")
            continue
        o, lat, idx = stage["overall"], stage["latency"], stage["index"]
        lines.append(
            f"| `{stage['stage']}`{mark} | {fmt(o.get('recall@20'))} | "
            f"{fmt(o.get('recall@100'))} | "
            f"{fmt(o.get('mrr'))} | {fmt(o.get('distractor_leak@10'))} | "
            f"{fmt(lat.get('p50_ms'), 2)} | {fmt(lat.get('p95_ms'), 2)} | "
            f"{fmt(idx.get('elapsed_s'), 2)} |"
        )
    lines += [
        "",
        f"> `{PRODUCTION_STAGE}` 即 v1.1 `DEFAULT_FLAGS` 的实际配置；"
        f"`{CONTROL_STAGE}` 是 v1.0 默认基线。`L6_temporal_intent_ctrl` 与 "
        "`L8_supersession_ctrl` 分别保留为时间意图放大负对照、无保护覆写安全对照。",
    ]

    lines += ["", "## 2. 难度分档对比（各档位 MRR）", ""]
    levels = sorted({lvl for s in payload["stages"] for lvl in (s.get("by_difficulty") or {})})
    if levels:
        header = " | ".join(f"MRR({lvl})" for lvl in levels)
        sep = " | ".join("---" for _ in levels)
        lines += [f"| 档位 | {header} |", f"| --- | {sep} |"]
        for stage in payload["stages"]:
            if stage["skipped"]:
                continue
            cells = " | ".join(
                fmt((stage.get("by_difficulty") or {}).get(lvl, {}).get("mrr"))
                for lvl in levels
            )
            lines.append(f"| `{stage['stage']}` | {cells} |")

    lines += ["", "## 3. 分查询类型明细（v1.1 代码默认档位）", ""]
    # 必须锁定 PRODUCTION_STAGE（代码默认），不能取"最后一个未跳过档位"。
    # 梯度里仍有多个后置实验档，不能用“最后一个未跳过档位”冒充生产配置。
    final = next(
        (s for s in payload["stages"] if s["stage"] == PRODUCTION_STAGE and not s["skipped"]),
        None,
    )
    fallback_note = ""
    if final is None:
        # 显式跳过了代码默认档位（如 --stages 白名单）时才回退，并明确标注。
        final = next((s for s in payload["stages"] if not s["skipped"]), None)
        if final is not None:
            fallback_note = (
                f"（⚠️ v1.1 代码默认档位 `{PRODUCTION_STAGE}` 本次未运行，"
                f"以下为回退展示的 `{final['stage']}`，**不代表代码默认**）"
            )
    if final:
        lines += [
            f"档位：`{final['stage']}`"
            + ("（**v1.1 代码默认 / production config**）" if not fallback_note else fallback_note),
            "",
            "| 查询类型 | 条数 | Recall@20 | Recall@100 | MRR | 旧值泄漏@10 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for kind, agg in final["by_kind"].items():
            note = agg.get("note")
            if note:
                lines.append(f"| {kind} | {agg.get('queries')} | — | — | — | — |"
                             f"  <!-- {note} -->")
                continue
            lines.append(
                f"| {kind} | {agg.get('queries')} | {fmt(agg.get('recall@20'))} | "
                f"{fmt(agg.get('recall@100'))} | {fmt(agg.get('mrr'))} | "
                f"{fmt(agg.get('distractor_leak@10'))} |"
            )

        control = next(
            (s for s in payload["stages"] if s["stage"] == CONTROL_STAGE and not s["skipped"]),
            None,
        )
        lines += [
            "",
            f"> **档位角色**：`{PRODUCTION_STAGE}` = v1.1 代码默认（`DEFAULT_FLAGS` 实配）；"
            f"`{CONTROL_STAGE}` = v1.0 对照基线（control），其 guarded supersession 默认**关闭**，"
            "用于量化 v1.1 变更，不参与 v1.1 线上返回。",
        ]
        if control:
            lines.append(
                f"> 对照组同口径整体指标：MRR={fmt(control['overall'].get('mrr'))}、"
                f"Recall@20={fmt(control['overall'].get('recall@20'))}"
                f"（对比代码默认 MRR={fmt(final['overall'].get('mrr'))}、"
                f"Recall@20={fmt(final['overall'].get('recall@20'))}）。"
            )

    lines += [
        "",
        "## 4. 指标口径",
        "",
        "- 结果可能是原始消息或聚合视图；只要某条结果的 `source_message_ids` 覆盖 gold 消息即算召回。",
        "- `Recall@k` = 前 k 条结果覆盖到的 gold 消息数 / gold 总数，按查询取平均。",
        "- `MRR` = 首个命中任一 gold 的结果排名倒数，未命中记 0。",
        "- `旧值泄漏@10` = 前 10 条中出现「已被覆写旧值」的比例，**越低越好**；"
        "系统不做删除，只做降权与冲突标注，故不为 0 属预期。",
        "- `absent` 类查询 gold 为空，不计入 Recall/MRR，仅用于观察系统是否硬凑证据。",
        "- 延迟为单进程串行 Search 的端到端墙钟时间（不含 HTTP 开销）。",
        "",
        "## 5. 复现命令",
        "",
        "```bash",
        f"cd {payload['repo']}",
        f"python3 scripts/run_eval.py --scale {ds['scale']} "
        f"--difficulty {ds['difficulty']} --suite {ds.get('suite', 'classic')} "
        f"--seed {ds['seed']} --top-k {payload['top_k']}",
        "```",
        "",
        "> 数据集完全由 `seed` 决定，同一 seed 必然复现同一份数据与同一组指标（延迟数除外）。",
        "",
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def fmt_agg(agg: dict | None, digits: int = 4) -> str:
    """把跨 seed 的 {mean,min,max} 渲染成 'mean (min–max)'。"""
    if not agg:
        return "—"
    mean = agg.get("mean")
    if mean is None:
        return "—"
    lo, hi = agg.get("min"), agg.get("max")
    if lo is None or hi is None:
        return fmt(mean, digits)
    return f"{fmt(mean, digits)} (min {fmt(lo, digits)} / max {fmt(hi, digits)})"


def write_multiseed_report(path: str, payload: dict) -> None:
    seeds = payload["seeds"]
    scale = payload["scale"]
    difficulty = payload["difficulty"]
    suite = payload.get("suite", "classic")
    top_k = payload["top_k"]
    aggregate = payload["aggregate"]
    per_seed = payload["per_seed"]
    agg_by_stage = {a["stage"]: a for a in aggregate}

    # 每个 seed 的 v1.1 代码默认档位指标，用于 §3 逐 seed 稳定性展示
    seed_summaries = []
    for ps in per_seed:
        for sr in ps:
            if sr["stage"] == PRODUCTION_STAGE and not sr["skipped"]:
                o = sr.get("overall") or {}
                lat = sr.get("latency") or {}
                seed_summaries.append({
                    "seed": sr.get("seed") or seeds[len(seed_summaries)],
                    "mrr": o.get("mrr"),
                    "recall@20": o.get("recall@20"),
                    "recall@100": o.get("recall@100"),
                    "distractor_leak@10": o.get("distractor_leak@10"),
                    "p50_ms": lat.get("p50_ms"),
                    "p95_ms": lat.get("p95_ms"),
                })
                break

    lines = [
        "# AML Retriever 跨 seed 聚合评测报告（多随机种子稳定性）",
        "",
        f"- 评测运行时间：{payload['generated_at']}"
        + (f"（报告重渲染于 {payload['rendered_at']}，指标未重算）"
           if payload.get("rendered_at") else ""),
        f"- 数据集：**纯合成**，suite=`{suite}`，scale=`{scale}`，difficulty=`{difficulty}`",
        f"- 随机种子：{seeds}（共 {len(seeds)} 个）",
        f"- top_k：{top_k}（官方正式评测口径）",
        f"- 运行环境：Python {payload['environment']['python']} / "
        f"SQLite {payload['environment']['sqlite_library_version']} / "
        f"FTS5={payload['environment']['fts5']}",
        "",
        "> 本报告的目的**不是**刷分，而是确认指标在合成数据随机种子之间是**稳定的**"
        "——即单 seed 上的结论（尤其 temporal×paraphrase 短板、L9 guarded supersession 是否缓解）"
        "并非某个 seed 的偶然。所有数字均为本机纯合成、零依赖、不联网。",
        "",
        "## 1. 跨 seed 聚合总览（各指标 mean / min / max）",
        "",
        "| 档位 | Recall@20 | Recall@100 | MRR | 旧值泄漏@10 | p50 (ms) | p95 (ms) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for a in aggregate:
        mark = " **(v1.1 代码默认)**" if a["stage"] == PRODUCTION_STAGE else ""
        if a["skipped"]:
            lines.append(f"| `{a['stage']}`{mark} | 跳过 | 跳过 | 跳过 | 跳过 | 跳过 | 跳过 |")
            continue
        o = a["overall"]
        lat = a.get("latency") or {}
        lines.append(
            f"| `{a['stage']}`{mark} | {fmt_agg(o.get('recall@20'))} | "
            f"{fmt_agg(o.get('recall@100'))} | {fmt_agg(o.get('mrr'))} | "
            f"{fmt_agg(o.get('distractor_leak@10'))} | "
            f"{fmt_agg(lat.get('p50_ms'), 2)} | {fmt_agg(lat.get('p95_ms'), 2)} |"
        )

    # §2：短板聚焦 —— temporal×paraphrase 交叉单元格的 L5/L8/L9 对比
    # 数据集 difficulty=plain 时不存在 paraphrase 难度，回退到 temporal|<difficulty>，
    # 并在标题里说明——否则整节会退化成三行「（无该交叉格）」，读者无法判断是缺数据还是没跑。
    available_cells = {k for a in aggregate for k in (a.get("by_kind_difficulty") or {})}
    cell_key = "temporal|paraphrase"
    if cell_key not in available_cells:
        fallback = f"temporal|{difficulty}"
        cell_key = fallback if fallback in available_cells else ""
    if cell_key == "temporal|paraphrase":
        focus_note = ("> 系统最弱的一环是「时间限定 + 查询被改写」：纯词法 + 确定性特征抓不到时间锚点"
                      "（见 docs/EVAL.md 附录 B）。该弱点在 **kind×difficulty 交叉表** 的 "
                      "`temporal|paraphrase` 单元格才暴露；看 `temporal` 整体会被其他难度稀释。")
    elif cell_key:
        focus_note = (f"> **注意**：本次 difficulty=`{difficulty}`，数据集中不存在 `paraphrase` 难度，"
                      f"因此下表展示的是回退单元格 `{cell_key}`，**不能**用来判断 temporal×paraphrase 短板；"
                      f"要看该短板请跑 `--difficulty paraphrase` 或 `--difficulty mixed`。")
    else:
        focus_note = ("> **注意**：本次运行未产出任何 `temporal|*` 交叉单元格（数据集不含 temporal 类查询），"
                      "本节无可对比数据。")
    lines += [
        "",
        f"## 2. 短板聚焦：{cell_key or 'temporal × paraphrase'} 交叉格",
        "",
        focus_note,
        "",
        "| 档位 | 角色 | MRR (mean/min/max) | Recall@20 (mean/min/max) | 旧值泄漏@10 |",
        "| --- | --- | --- | --- | --- |",
    ]
    role_of = {
        PRODUCTION_STAGE: "v1.1 代码默认",
        CONTROL_STAGE: "v1.0 基线",
        "L8_supersession_ctrl": "无保护安全对照",
    }
    for stage in (PRODUCTION_STAGE, CONTROL_STAGE, "L8_supersession_ctrl"):
        a = agg_by_stage.get(stage)
        if not a or a["skipped"]:
            continue
        cell = (a["by_kind_difficulty"] or {}).get(cell_key) if cell_key else None
        if not cell:
            lines.append(f"| `{stage}` | {role_of.get(stage, '—')} | （无该交叉格）| — | — |")
            continue
        lines.append(
            f"| `{stage}` | {role_of.get(stage, '—')} | {fmt_agg(cell.get('mrr'))} | "
            f"{fmt_agg(cell.get('recall@20'))} | {fmt_agg(cell.get('distractor_leak@10'))} |"
        )
    lines += [
        "",
        "> **v1.1 说明**：`L8_supersession_ctrl` 只看话题重合与时间，作为无保护安全对照；"
        f"`{PRODUCTION_STAGE}` 进一步要求显式更新语义，并使用保守 4/1 权重。两者都只做软重排，"
        "不安装依赖、不做 confirmed-only 过滤，也不删除旧证据。",
        f"> 只有 `{PRODUCTION_STAGE}` 在跨 seed 上同时守住召回门并提升 MRR，才可标记为 v1.1 默认；"
        "官方数据上的效果仍必须写为 unknown，不能用本合成代理集代替官方验证。",
        "",
        "## 3. 逐 seed 稳定性（v1.1 代码默认档位 " + PRODUCTION_STAGE + "）",
        "",
        "| seed | Recall@20 | Recall@100 | MRR | 旧值泄漏@10 | p50 (ms) | p95 (ms) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in seed_summaries:
        lines.append(
            f"| {s['seed']} | {fmt(s['recall@20'])} | {fmt(s['recall@100'])} | "
            f"{fmt(s['mrr'])} | {fmt(s['distractor_leak@10'])} | "
            f"{fmt(s['p50_ms'], 2)} | {fmt(s['p95_ms'], 2)} |"
        )
    lines += [
        "",
        "> 若上表各 seed 的 MRR / Recall@20 接近，则结论在 seed 间稳定；"
        "差异较大则说明该结论对合成数据随机性敏感，需谨慎外推到官方数据（属 `unknown`）。",
        "",
        "## 4. 指标口径与复现",
        "",
        "- `Recall@k` = 前 k 条覆盖到的 gold 消息数 / gold 总数，按查询取平均；"
        "结果为原始消息或聚合视图皆可，只要 `source_message_ids` 覆盖 gold 即算命中。",
        "- `MRR` = 首个命中任一 gold 的结果排名倒数，未命中记 0。",
        "- `旧值泄漏@10` = 前 10 条出现「已被覆写旧值」的比例，越低越好；"
        "系统不删旧值，只降权与冲突标注，故不为 0 属预期。",
        "- `p50/p95 (ms)` = 单次 search 的端到端耗时分位数（本机、冷缓存、单进程），"
        "跨 seed 聚合的是各 seed 自身的分位数再取 mean/min/max，**不是**把所有 seed 的原始延迟合池后取分位。",
        "- 跨 seed 聚合：`mean` 为各 seed 算术平均，`min/max` 为各 seed 极值，`n` 为参与聚合的 seed 数。",
        "- 产物只含指标数字，**不落任何语料原文**；逐 seed 明细见同目录 "
        f"`ablation_{scale}_{difficulty}_multiseed_per_seed.csv`。临时索引库跑完即删。",
        "",
        "```bash",
        f"cd {payload['repo']}",
        f"python3 scripts/run_eval.py --scale {scale} --difficulty {difficulty} "
        f"--suite {suite} --seeds {','.join(str(s) for s in seeds)} --top-k {top_k}",
        "```",
        "",
        "> 数据集完全由 `seed` 决定，同一组 seed 必然复现同一组指标（延迟数除外）。",
        "",
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def write_multiseed_artifacts(out_dir: str, payload: dict) -> list[str]:
    """把跨 seed 评测的 payload 落成 JSON / 聚合 CSV / 逐 seed CSV / Markdown 报告。

    与实际跑评测解耦，因此 ``--from-json`` 可以在**不重跑评测**的前提下，用新的
    报告模板重新渲染历史 payload（改报告格式不该花 15 分钟重算指标）。
    返回写出的文件路径列表。
    """
    # Reports are portable public artifacts; never serialize a checkout path.
    payload["repo"] = PUBLIC_REPO

    scale, difficulty = payload["scale"], payload["difficulty"]
    suite = payload.get("suite", "classic")
    seeds = payload["seeds"]

    # 防御性清洗：老 payload（harness 修复前生成）在 index.rows 里带本机临时库路径。
    # 临时库跑完即删，路径落进提交产物只是噪声，重渲染时一并剥掉。
    for rows in payload.get("per_seed", []):
        for r in rows:
            (r.get("index") or {}).get("rows", {}).pop("db_path", None)

    suite_tag = "" if suite == "classic" else f"_{suite}"
    tag = f"{scale}_{difficulty}{suite_tag}_multiseed"
    json_path = os.path.join(out_dir, f"ablation_{tag}.json")
    csv_path = os.path.join(out_dir, f"ablation_{tag}.csv")
    per_seed_csv = os.path.join(out_dir, f"ablation_{tag}_per_seed.csv")
    md_path = os.path.join(out_dir, f"REPORT_{tag}.md")

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    # 跨 seed 聚合 CSV：每个档位一行，质量指标 + 延迟指标各自的 mean/min/max
    quality_keys = ("recall@20", "recall@100", "mrr", "distractor_leak@10")
    latency_keys = ("p50_ms", "p95_ms")
    agg_cols = ["stage", "skipped", "n_seeds"]
    for k in quality_keys + latency_keys:
        agg_cols += [f"{k}_mean", f"{k}_min", f"{k}_max"]
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(agg_cols)
        for a in payload["aggregate"]:
            row = [a["stage"], int(bool(a["skipped"])), len(seeds)]
            for k in quality_keys:
                cell = (a.get("overall") or {}).get(k) or {}
                row += [cell.get("mean"), cell.get("min"), cell.get("max")]
            for k in latency_keys:
                cell = (a.get("latency") or {}).get(k) or {}
                row += [cell.get("mean"), cell.get("min"), cell.get("max")]
            writer.writerow(row)

    # 逐 seed 明细 CSV：每 (seed, 档位) 一行，保证「每个 seed 的原始数字」可核查，
    # 不含任何语料原文，只有指标。
    with open(per_seed_csv, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["seed", "stage", "skipped", *quality_keys, *latency_keys])
        for rows in payload["per_seed"]:
            for r in rows:
                o, lat = r.get("overall") or {}, r.get("latency") or {}
                writer.writerow([
                    r.get("seed"), r["stage"], int(bool(r["skipped"])),
                    *[o.get(k) for k in quality_keys],
                    *[lat.get(k) for k in latency_keys],
                ])

    write_multiseed_report(md_path, payload)
    return [json_path, csv_path, per_seed_csv, md_path]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AML Retriever 离线消融评测")
    parser.add_argument("--scale", default="small", choices=sorted(SCALES),
                        help="数据规模（默认 small）")
    parser.add_argument("--seed", type=int, default=20260806, help="数据集随机种子（单 seed 模式）")
    parser.add_argument("--seeds", default="", help="逗号分隔的多个随机种子，启用跨 seed 聚合模式")
    parser.add_argument("--difficulty", default="mixed", choices=list(DIFFICULTIES),
                        help="查询难度：plain=词面重叠 / paraphrase=改写 / mixed=各半")
    parser.add_argument("--suite", default="classic", choices=list(SUITES),
                        help="评测套件：classic=原基准 / v11=原基准加更新与偏好代理题")
    parser.add_argument("--top-k", type=int, default=OFFICIAL_TOP_K, help="检索 top_k")
    parser.add_argument("--out", default="eval_out", help="产物输出目录")
    parser.add_argument("--stages", default="", help="逗号分隔的档位白名单")
    parser.add_argument("--dump-dataset", default="", help="把完整数据集导出到指定 JSON")
    parser.add_argument("--from-json", default="",
                        help="不重跑评测，直接用已有的 multiseed JSON payload 重新渲染 CSV/报告")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = args.out if os.path.isabs(args.out) else os.path.join(repo, args.out)
    os.makedirs(out_dir, exist_ok=True)

    # ---- 仅重渲染模式（不跑评测）------------------------------------------
    if args.from_json:
        src = args.from_json if os.path.isabs(args.from_json) else os.path.join(repo, args.from_json)
        with open(src, encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload.get("mode") != "multiseed":
            parser.error(f"--from-json 只支持 multiseed payload，实际 mode={payload.get('mode')!r}")
        payload["rendered_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        for p in write_multiseed_artifacts(out_dir, payload):
            if not args.quiet:
                print(f"[eval] re-rendered {p}")
        return 0

    # ---- 跨 seed 聚合模式 -------------------------------------------------
    seeds = [int(x) for x in args.seeds.split(",") if x.strip()] if args.seeds else []
    if seeds:
        if len(seeds) < 1:
            parser.error("--seeds 至少需要一个种子")
        ladder = ABLATION_LADDER
        if args.stages:
            wanted = {s.strip() for s in args.stages.split(",") if s.strip()}
            ladder = [item for item in ABLATION_LADDER if item[0] in wanted]
            if not ladder:
                parser.error(f"no stage matched: {sorted(wanted)}")

        def on_stage(result):
            if args.quiet:
                return
            if result.skipped:
                print(f"[eval] {result.stage:24s} SKIPPED — {result.skip_reason}")
            else:
                o, lat = result.overall, result.latency
                print(f"[eval] {result.stage:24s} R@20={o.get('recall@20')} "
                      f"R@100={o.get('recall@100')} MRR={o.get('mrr')} "
                      f"p95={lat.get('p95_ms')}ms")

        started = time.time()
        multi = run_ladder_seeds(seeds, scale=args.scale, difficulty=args.difficulty,
                                 suite=args.suite,
                                 top_k=args.top_k, ladder=ladder, on_stage=on_stage)

        # per_seed 转 dict 以便 JSON 序列化，并注入 seed 便于报告溯源
        per_seed_dict = []
        for seed, ps in zip(multi["seeds"], multi["per_seed"]):
            per_seed_dict.append([{**r.to_dict(), "seed": seed} for r in ps])
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "repo": PUBLIC_REPO,
            "mode": "multiseed",
            "seeds": seeds,
            "scale": args.scale,
            "difficulty": args.difficulty,
            "suite": args.suite,
            "top_k": args.top_k,
            "wall_clock_s": round(time.time() - started, 2),
            "environment": environment(),
            "per_seed": per_seed_dict,
            "aggregate": multi["aggregate"],
        }

        written = write_multiseed_artifacts(out_dir, payload)
        if not args.quiet:
            for p in written:
                print(f"[eval] wrote {p}")
        return 0

    dataset = make_dataset(
        seed=args.seed,
        scale=args.scale,
        difficulty=args.difficulty,
        suite=args.suite,
    )
    if args.dump_dataset:
        dump_path = (args.dump_dataset if os.path.isabs(args.dump_dataset)
                     else os.path.join(repo, args.dump_dataset))
        os.makedirs(os.path.dirname(dump_path) or ".", exist_ok=True)
        with open(dump_path, "w", encoding="utf-8") as fh:
            json.dump(dataset.dump(), fh, ensure_ascii=False, indent=2)

    ladder = ABLATION_LADDER
    if args.stages:
        wanted = {s.strip() for s in args.stages.split(",") if s.strip()}
        ladder = [item for item in ABLATION_LADDER if item[0] in wanted]
        if not ladder:
            parser.error(f"no stage matched: {sorted(wanted)}")

    if not args.quiet:
        print(f"[eval] dataset={dataset.to_dict()}")

    def on_stage(result):
        if args.quiet:
            return
        if result.skipped:
            print(f"[eval] {result.stage:24s} SKIPPED — {result.skip_reason}")
        else:
            o, lat = result.overall, result.latency
            print(f"[eval] {result.stage:24s} R@20={o.get('recall@20')} "
                  f"R@100={o.get('recall@100')} MRR={o.get('mrr')} "
                  f"p95={lat.get('p95_ms')}ms")

    workdir = os.path.join(out_dir, "_work")
    os.makedirs(workdir, exist_ok=True)
    started = time.time()
    results = run_ladder(dataset, workdir=workdir, top_k=args.top_k,
                         ladder=ladder, on_stage=on_stage)

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repo": PUBLIC_REPO,
        "top_k": args.top_k,
        "wall_clock_s": round(time.time() - started, 2),
        "environment": environment(),
        "dataset": dataset.to_dict(),
        "stages": [r.to_dict() for r in results],
    }

    suite_tag = "" if args.suite == "classic" else f"_{args.suite}"
    tag = f"{args.scale}_{args.difficulty}{suite_tag}_{args.seed}"
    json_path = os.path.join(out_dir, f"ablation_{tag}.json")
    csv_path = os.path.join(out_dir, f"ablation_{tag}.csv")
    md_path = os.path.join(out_dir, f"REPORT_{args.scale}_{args.difficulty}{suite_tag}.md")

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for result in results:
            writer.writerow(to_csv_row(result))
    write_report(md_path, payload)

    # 清理临时库，只留产物
    for name in os.listdir(workdir):
        try:
            os.remove(os.path.join(workdir, name))
        except OSError:
            pass
    try:
        os.rmdir(workdir)
    except OSError:
        pass

    if not args.quiet:
        print(f"[eval] wrote {json_path}")
        print(f"[eval] wrote {csv_path}")
        print(f"[eval] wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
