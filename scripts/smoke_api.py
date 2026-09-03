#!/usr/bin/env python3
"""官方 Add/Search 契约的 HTTP smoke（真实起服务、真实发请求）。

默认自起一个临时服务器 + 临时库，跑完自动清理，不留数据：

    python3 scripts/smoke_api.py

也可以打到一个已经在跑的实例（此时使用你自己的库，注意数据留存）：

    python3 scripts/smoke_api.py --base-url http://127.0.0.1:8080 --api-key xxx

校验点严格对齐 docs/API_CONTRACT.md：
  - GET  /health   无需鉴权，2xx
  - POST /add      返回 {success:true, request_id, user_id, session_id} 三 ID 原样回显
  - 同步语义       Add 返回后**立即** Search 必须能查到
  - POST /search   返回 {data:[{id, content, ...}]}，无 items 包装、非顶层数组
  - top_k          返回条数 ≤ top_k
  - user_id 隔离   跨 user 查不到
  - 幂等           同 request_id 重复 Add 不重复落库
  - 422            缺字段 / 空 content / 缺 role / 缺 top_k / 非整数 top_k /
                   小数 timestamp 一律被拒，且错误体点名字段
  - 宽进           官方未声明的额外字段（顶层与 message 内）只忽略不报错
退出码 0 = 全部通过。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, note: str = "") -> None:
    RESULTS.append((name, bool(passed), note))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f"  ({note})" if note else ""))


def call(base: str, path: str, payload=None, *, api_key: str = "", method: str = "POST"):
    url = base.rstrip("/") + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def run(base: str, api_key: str) -> bool:
    user = f"smoke:{uuid.uuid4().hex[:8]}"
    session = f"{user}:sess-0"

    status, body = call(base, "/health", method="GET")
    check("GET /health 返回 2xx", 200 <= status < 300, f"HTTP {status}")

    add_body = {
        "request_id": f"{user}:chunk-0",
        "user_id": user,
        "session_id": session,
        "messages": [
            {"role": "user", "timestamp": 1704067200000,
             "content": "我把发布日期定在 2026-08-14，负责人是林涛。"},
            {"role": "assistant", "timestamp": 1704067260000,
             "content": "好的，已记录发布日期 2026-08-14，负责人林涛。"},
            {"role": "user", "timestamp": 1704153600000,
             "content": "更新一下，发布日期改成 2026-08-21。"},
        ],
    }
    status, body = call(base, "/add", add_body, api_key=api_key)
    check("POST /add 返回 200", status == 200, f"HTTP {status}")
    check("add.success 为布尔 true", body.get("success") is True, repr(body.get("success")))
    check("add 原样回显三个 ID",
          body.get("request_id") == add_body["request_id"]
          and body.get("user_id") == user
          and body.get("session_id") == session)
    check("add 未返回 202/任务 ID", status != 202 and "task_id" not in body)

    # 同步语义：Add 刚返回就必须能搜到
    status, body = call(base, "/search",
                        {"query": "发布日期 负责人", "user_id": user, "top_k": 100},
                        api_key=api_key)
    check("POST /search 返回 200", status == 200, f"HTTP {status}")
    check("search 返回 data 数组", isinstance(body.get("data"), list))
    check("search 无 items 包装层", "items" not in body)
    check("search 非顶层数组", isinstance(body, dict))
    data = body.get("data") or []
    check("写后立即可搜（同步语义）", len(data) > 0, f"{len(data)} 条")
    check("每条含非空 id / content",
          all(isinstance(d.get("id"), str) and d["id"]
              and isinstance(d.get("content"), str) and d["content"] for d in data))
    check("返回原始证据而非生成答案",
          any("2026-08-21" in d.get("content", "") or "林涛" in d.get("content", "")
              for d in data))

    status, body = call(base, "/search",
                        {"query": "发布日期", "user_id": user, "top_k": 2}, api_key=api_key)
    check("top_k 上限被遵守", len(body.get("data") or []) <= 2,
          f"{len(body.get('data') or [])} 条")

    status, body = call(base, "/search",
                        {"query": "发布日期 负责人", "user_id": f"{user}-other", "top_k": 100},
                        api_key=api_key)
    check("user_id 隔离", (body.get("data") or []) == [])

    status, body = call(base, "/add", add_body, api_key=api_key)
    check("重复 request_id 仍返回 200", status == 200, f"HTTP {status}")
    status, body = call(base, "/search",
                        {"query": "发布日期 负责人", "user_id": user, "top_k": 100},
                        api_key=api_key)
    check("幂等：重复 Add 未翻倍", len(body.get("data") or []) == len(data),
          f"{len(body.get('data') or [])} vs {len(data)}")

    status, body = call(base, "/add", {"user_id": user}, api_key=api_key)
    check("缺字段返回 422", status == 422, f"HTTP {status}")
    check("错误体为 {detail:{reason}}", isinstance(body.get("detail"), dict)
          and "reason" in body["detail"])
    status, body = call(base, "/search", {"user_id": user, "top_k": 10}, api_key=api_key)
    check("search 缺 query 返回 422", status == 422, f"HTTP {status}")

    # -- 严格契约负向用例（2026-08-07 收紧）--------------------------------
    no_role = json.loads(json.dumps(add_body))
    no_role["request_id"] = f"{user}:neg-role"
    no_role["messages"][0].pop("role")
    status, body = call(base, "/add", no_role, api_key=api_key)
    check("add 缺 role 返回 422", status == 422, f"HTTP {status}")
    check("422 原因指明 role", "role" in str((body.get("detail") or {}).get("reason", "")))

    blank_role = json.loads(json.dumps(add_body))
    blank_role["request_id"] = f"{user}:neg-role-blank"
    blank_role["messages"][0]["role"] = "  "
    status, _ = call(base, "/add", blank_role, api_key=api_key)
    check("add 空白 role 返回 422", status == 422, f"HTTP {status}")

    frac_ts = json.loads(json.dumps(add_body))
    frac_ts["request_id"] = f"{user}:neg-ts"
    frac_ts["messages"][0]["timestamp"] = 1704067200000.5
    status, _ = call(base, "/add", frac_ts, api_key=api_key)
    check("add 小数 timestamp 返回 422（不静默截断）", status == 422, f"HTTP {status}")

    status, body = call(base, "/search", {"query": "发布日期", "user_id": user}, api_key=api_key)
    check("search 缺 top_k 返回 422", status == 422, f"HTTP {status}")
    check("422 原因指明 top_k", "top_k" in str((body.get("detail") or {}).get("reason", "")))

    for label, bad in (("小数", 10.5), ("字符串", "10"), ("布尔", True)):
        status, _ = call(base, "/search",
                         {"query": "发布日期", "user_id": user, "top_k": bad}, api_key=api_key)
        check(f"search {label} top_k 返回 422", status == 422, f"HTTP {status}")

    status, body = call(base, "/search",
                        {"query": "发布日期", "user_id": user, "top_k": 100,
                         "session_id": session, "rerank": True, "filters": {"x": 1}},
                        api_key=api_key)
    check("search 容忍未声明的额外字段", status == 200, f"HTTP {status}")

    tolerant = json.loads(json.dumps(add_body))
    tolerant["request_id"] = f"{user}:extra-fields"
    tolerant["messages"][0]["name"] = "alice"
    tolerant["app_id"] = "whatever"
    status, _ = call(base, "/add", tolerant, api_key=api_key)
    check("add 容忍未声明的额外字段", status == 200, f"HTTP {status}")

    # 清理本次 smoke 产生的数据
    status, body = call(base, "/admin/delete_user", {"user_id": user}, api_key=api_key)
    check("清理 smoke 数据", status == 200, str(body))

    return all(p for _, p, _ in RESULTS)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="官方 Add/Search 契约 HTTP smoke")
    parser.add_argument("--base-url", default="", help="已在运行的实例地址；留空则自起临时服务")
    parser.add_argument("--api-key", default="", help="Bearer 凭据（可选）")
    parser.add_argument("--port", type=int, default=0, help="自起服务端口，0=随机")
    args = parser.parse_args(argv)

    if args.base_url:
        ok = run(args.base_url, args.api_key)
    else:
        from aml_retriever.config import RetrieverConfig
        from aml_retriever.server import RetrieverServer

        tmpdir = tempfile.mkdtemp(prefix="aml-smoke-")
        config = RetrieverConfig(db_path=os.path.join(tmpdir, "smoke.db"),
                                 host="127.0.0.1", port=args.port)
        httpd = RetrieverServer(config, quiet=True)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        print(f"[smoke] 临时服务 http://127.0.0.1:{port}  db={config.db_path}")
        try:
            ok = run(f"http://127.0.0.1:{port}", "")
        finally:
            httpd.shutdown()
            httpd.server_close()
            httpd.service.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            print("[smoke] 临时库已删除")

    passed = sum(1 for _, p, _ in RESULTS if p)
    print(f"\nsmoke: {'OK' if ok else 'FAILED'} — {passed}/{len(RESULTS)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
