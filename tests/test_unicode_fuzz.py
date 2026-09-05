"""Deterministic Unicode properties for both lexical search implementations."""
from __future__ import annotations

import random
import unittest

from aml_retriever.config import RetrieverConfig
from aml_retriever.features import query_tokens
from aml_retriever.retriever import RetrieverDB
from aml_retriever.store import Store, _build_match, tokenize


SEED = 20260906
CASES = 320
MAX_LENGTH = 512
_ALPHABET = (
    "abcXYZ019 北京上海 記憶메모리 "
    "\"'`\\/\x00\n\r\t"
    "éΩЖक् e\u0301 １２３"
    "😀🧠🚀\U00020000"
    "\ud800\udfff"
)
_CURATED = (
    "",
    "\x00",
    '"""',
    "' OR 1 --",
    "北京\x00memory😀",
    "e\u0301éＥxample１２３",
    "\ud800",
    "a\ud800b\udfff9",
    "\U00020000野家" * 32,
    "a" * MAX_LENGTH,
)


def unicode_cases() -> tuple[str, ...]:
    """Return a stable, bounded corpus; failures reproduce from case index."""
    rng = random.Random(SEED)
    generated = []
    for _ in range(CASES - len(_CURATED)):
        length = rng.randrange(MAX_LENGTH + 1)
        generated.append("".join(rng.choice(_ALPHABET) for _ in range(length)))
    return _CURATED + tuple(generated)


def safe_case(value: str) -> str:
    return value.encode("unicode_escape", "backslashreplace").decode("ascii")


class TestUnicodeLexicalProperties(unittest.TestCase):
    def test_generator_is_deterministic_and_bounded(self):
        first = unicode_cases()
        second = unicode_cases()
        self.assertEqual(first, second)
        self.assertEqual(len(first), CASES)
        self.assertTrue(all(isinstance(value, str) and len(value) <= MAX_LENGTH for value in first))

    def test_legacy_tokenizer_match_and_sqlite_never_raise(self):
        with Store(":memory:") as store:
            store.add(
                request_id="unicode-fuzz-source",
                user_id="u1",
                content="unicode fuzz anchor 北京 memory 123",
            )
            for index, value in enumerate(unicode_cases()):
                with self.subTest(index=index, value=safe_case(value)):
                    tokens = tokenize(value)
                    self.assertTrue(all(token and '"' not in token for token in tokens))
                    expression = _build_match(value)
                    if expression is not None:
                        self.assertEqual(expression.count('"') % 2, 0)
                    result = store.search(user_id="u1", query=value, top_k=10)
                    self.assertLessEqual(len(result.results), 10)

    def test_product_tokenizer_match_and_sqlite_never_raise(self):
        database = RetrieverDB(RetrieverConfig(db_path=":memory:"))
        try:
            database.add(
                request_id="unicode-fuzz-source",
                user_id="u1",
                session_id="s1",
                messages=[{"role": "user", "content": "unicode fuzz anchor 北京 memory 123"}],
            )
            for index, value in enumerate(unicode_cases()):
                with self.subTest(index=index, value=safe_case(value)):
                    tokens = query_tokens(value)
                    self.assertLessEqual(len(tokens), database.config.max_query_tokens)
                    expression = database._match_expr(tokens)
                    if expression is not None:
                        self.assertEqual(expression.count('"') % 2, 0)
                    result = database.search(user_id="u1", query=value, top_k=10)
                    self.assertLessEqual(len(result.results), 10)
        finally:
            database.close()


if __name__ == "__main__":
    unittest.main()
