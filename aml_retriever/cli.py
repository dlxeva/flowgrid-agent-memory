"""AML Retriever 命令行入口（仅标准库）。

子命令::

  serve         启动官方 Add/Search HTTP wrapper
  add           写入一次 Add（官方形状：一次 Add 可含多条 message）
  search        执行一次 Search
  delete-user   删除某个 user_id 的全部数据（原始消息 + 视图 + 索引 + 幂等记录）
  purge         清空整库（删除全部 user 的数据）
  stats         打印行数统计（不含任何记忆内容）
  selfcheck     无副作用自检：写入→检索→删除，验证端到端可用

示例::

  python3 -m aml_retriever.cli serve --db aml.db --port 8080
  python3 -m aml_retriever.cli add --db aml.db --user u1 --session s1 --request r1 \
      --content "北京 2024-01-02 会议：准确率 0.95"
  python3 -m aml_retriever.cli search --db aml.db --user u1 --query "北京" --top-k 5
  python3 -m aml_retriever.cli delete-user --db aml.db --user u1

隐私：本 CLI 只在用户显式执行 add/search 时于 stdout 回显内容；
``stats`` / ``delete-user`` / ``purge`` / ``serve`` 均不打印任何记忆正文。
"""
from __future__ import annotations

import argparse
import json
import sys

from .api import MemoryService
from .config import RetrieverConfig


def _config(args) -> RetrieverConfig:
    cfg = RetrieverConfig.from_env(getattr(args, "config", None))
    if getattr(args, "db", None):
        cfg.db_path = args.db
    for attr in ("host", "port", "auth_mode", "api_key"):
        value = getattr(args, attr, None)
        if value not in (None, ""):
            setattr(cfg, attr, value)
    return cfg


def _emit(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cmd_serve(args) -> int:
    from .server import serve  # 延迟导入，避免非 serve 场景加载 http 栈

    serve(_config(args), quiet=args.quiet)
    return 0


def _cmd_add(args) -> int:
    with MemoryService(_config(args)) as svc:
        body = {
            "request_id": args.request,
            "user_id": args.user,
            "session_id": args.session,
            "messages": [{"role": args.role, "content": args.content}],
        }
        if args.timestamp is not None:
            body["messages"][0]["timestamp"] = args.timestamp
        _emit(svc.official_add(body))
    return 0


def _cmd_search(args) -> int:
    with MemoryService(_config(args)) as svc:
        _emit(svc.official_search({
            "query": args.query,
            "user_id": args.user,
            "top_k": args.top_k,
        }))
    return 0


def _cmd_delete_user(args) -> int:
    with MemoryService(_config(args)) as svc:
        _emit(svc.delete_user(args.user))
    return 0


def _cmd_purge(args) -> int:
    if not args.yes:
        print("拒绝执行：purge 会清空整库，请显式加 --yes 确认。", file=sys.stderr)
        return 2
    with MemoryService(_config(args)) as svc:
        _emit(svc.db.purge_all())
    return 0


def _cmd_stats(args) -> int:
    with MemoryService(_config(args)) as svc:
        _emit(svc.stats())
    return 0


def _cmd_selfcheck(args) -> int:
    """端到端自检：全程使用独立的临时 user_id，结束后删除，不污染现有数据。"""
    import tempfile
    import uuid

    probe_user = f"selfcheck-{uuid.uuid4().hex[:8]}"
    cfg = _config(args)
    tmpdir = None
    if cfg.db_path in ("", ":memory:") and not args.db:
        tmpdir = tempfile.mkdtemp(prefix="aml-selfcheck-")
        cfg.db_path = f"{tmpdir}/probe.db"

    checks: list[tuple[str, bool, str]] = []
    try:
        with MemoryService(cfg) as svc:
            added = svc.official_add({
                "request_id": f"{probe_user}-r1",
                "user_id": probe_user,
                "session_id": f"{probe_user}-s1",
                "messages": [{"role": "user", "content": "自检消息：订单 A7731 于 2026-08-06 发出"}],
            })
            checks.append(("add 返回 success=true", added.get("success") is True, str(added.get("success"))))
            checks.append(("add 回显 request_id", added.get("request_id") == f"{probe_user}-r1", ""))

            found = svc.official_search({"query": "A7731", "user_id": probe_user, "top_k": 10})
            data = found.get("data") or []
            checks.append(("写后立即可搜", len(data) > 0, f"{len(data)} 条"))
            checks.append(("返回原始证据", any("A7731" in (d.get("content") or "") for d in data), ""))

            other = svc.official_search({"query": "A7731", "user_id": "someone-else", "top_k": 10})
            checks.append(("user_id 隔离", (other.get("data") or []) == [], ""))

            again = svc.official_add({
                "request_id": f"{probe_user}-r1",
                "user_id": probe_user,
                "session_id": f"{probe_user}-s1",
                "messages": [{"role": "user", "content": "自检消息：订单 A7731 于 2026-08-06 发出"}],
            })
            checks.append(("request_id 幂等", again.get("success") is True, ""))
            checks.append(("幂等未重复落库",
                           svc.db.count(probe_user) == 1, f"count={svc.db.count(probe_user)}"))

            removed = svc.delete_user(probe_user)
            after = svc.official_search({"query": "A7731", "user_id": probe_user, "top_k": 10})
            checks.append(("删除后不可检索", (after.get("data") or []) == [], str(removed)))
    finally:
        if tmpdir:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)

    ok = all(passed for _, passed, _ in checks)
    for name, passed, note in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f"  ({note})" if note else ""))
    print(f"\nselfcheck: {'OK' if ok else 'FAILED'} — {sum(p for _, p, _ in checks)}/{len(checks)}")
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="aml_retriever", description="AML Retriever CLI")
    p.add_argument("--config", default=None, help="JSON 配置文件路径（也可用 AML_CONFIG）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(parser, *, need_db=True):
        parser.add_argument("--db", required=False, default=None,
                            help="SQLite 文件路径（也可用 AML_DB_PATH）")

    sv = sub.add_parser("serve", help="启动官方 Add/Search HTTP wrapper")
    common(sv)
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    sv.add_argument("--auth-mode", dest="auth_mode", default=None,
                    choices=["none", "bearer", "token", "x-api-key"])
    sv.add_argument("--api-key", dest="api_key", default=None)
    sv.add_argument("--quiet", action="store_true", help="不打印访问日志")
    sv.set_defaults(func=_cmd_serve)

    a = sub.add_parser("add", help="写入一条消息证据")
    common(a)
    a.add_argument("--user", required=True)
    a.add_argument("--session", default="cli-session")
    a.add_argument("--request", required=True)
    a.add_argument("--content", required=True)
    a.add_argument("--role", default="user")
    a.add_argument("--timestamp", type=int, default=None, help="Unix 毫秒")
    a.set_defaults(func=_cmd_add)

    s = sub.add_parser("search", help="按查询检索证据")
    common(s)
    s.add_argument("--user", required=True)
    s.add_argument("--query", required=True)
    s.add_argument("--top-k", type=int, default=10)
    s.set_defaults(func=_cmd_search)

    d = sub.add_parser("delete-user", help="删除某个 user_id 的全部数据")
    common(d)
    d.add_argument("--user", required=True)
    d.set_defaults(func=_cmd_delete_user)

    pg = sub.add_parser("purge", help="清空整库（需 --yes）")
    common(pg)
    pg.add_argument("--yes", action="store_true")
    pg.set_defaults(func=_cmd_purge)

    st = sub.add_parser("stats", help="打印行数统计（无记忆内容）")
    common(st)
    st.set_defaults(func=_cmd_stats)

    sc = sub.add_parser("selfcheck", help="端到端自检（写入→检索→隔离→幂等→删除）")
    common(sc)
    sc.set_defaults(func=_cmd_selfcheck)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
