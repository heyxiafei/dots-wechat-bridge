"""Offline differential checks against pinned CowAgent pure-function AST.

Copyright (c) 2022 zhayujie; MIT, see licenses/CowAgent-MIT.txt.
The reference strings contain only reviewed pure functions, never imports or I/O.
"""
import ast
import hashlib
import random
import unittest

from foundation.weixin_protocol import aes_ecb_padded_size, split_text

UPSTREAM_SPLIT_SOURCE = 'def _split_text(text: str, limit: int) -> list:\n    """Split text into chunks, preferring to break at paragraph or line boundaries."""\n    if len(text) <= limit:\n        return [text]\n    chunks = []\n    while text:\n        if len(text) <= limit:\n            chunks.append(text)\n            break\n        cut = text.rfind(\'\\n\\n\', 0, limit)\n        if cut <= 0:\n            cut = text.rfind(\'\\n\', 0, limit)\n        if cut <= 0:\n            cut = limit\n        chunks.append(text[:cut])\n        text = text[cut:].lstrip(\'\\n\')\n    return chunks\n'
UPSTREAM_PAD_SOURCE = 'def _aes_ecb_padded_size(plaintext_size: int) -> int:\n    """PKCS7 padded size for AES-128-ECB."""\n    return (plaintext_size + 1 + 15) // 16 * 16\n'
REFERENCE_HASHES = {'_split_text': '3c8be9102b69c808d8b5c4ff5008f82b07a3d86292f7913d36cb2926bc997c2f', '_aes_ecb_padded_size': 'c3e275a004980b8808137b26111c9b17bf77b2c952385ee191bea95edaf086d4'}


def load_pure_reference(source, name):
    tree = ast.parse(source)
    # Prevent the differential fixture from gaining imports, file/network access
    # or module-level execution while keeping its exact pinned upstream body.
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise AssertionError("reference_is_not_one_function")
    if tree.body[0].name != name or tree.body[0].decorator_list:
        raise AssertionError("reference_identity_changed")
    if hashlib.sha256(source.encode()).hexdigest() != REFERENCE_HASHES[name]:
        raise AssertionError("pinned_reference_changed")
    scope = {"__builtins__": {"len": len, "str": str, "int": int, "list": list}}
    exec(compile(tree, "<pinned-cowagent-pure-function>", "exec"), scope)
    return scope[name]


class CowAgentPureProtocolTests(unittest.TestCase):
    def test_split_matches_pinned_upstream(self):
        upstream = load_pure_reference(UPSTREAM_SPLIT_SOURCE, "_split_text")
        rng = random.Random(20261106)
        cases = ["", "a", "a\nb", "a\n\nb", "\n\nhello\nworld", "你好🙂" * 4000]
        cases += ["".join(rng.choice("ab\n 你好🙂") for _ in range(rng.randrange(1200))) for _ in range(120)]
        for text in cases:
            for limit in [1, 2, 3, 7, 32, 3999, 4000]:
                with self.subTest(length=len(text), limit=limit):
                    actual = split_text(text, limit)
                    self.assertEqual(actual, upstream(text, limit))
                    self.assertTrue(all(len(chunk) <= limit for chunk in actual))

    def test_pkcs7_size_matches_upstream_and_block_boundaries(self):
        upstream = load_pure_reference(UPSTREAM_PAD_SOURCE, "_aes_ecb_padded_size")
        for size in list(range(8193)) + [2**20, 2**30, 2**53]:
            with self.subTest(size=size):
                actual = aes_ecb_padded_size(size)
                self.assertEqual(actual, upstream(size))
                self.assertEqual(actual % 16, 0)
                self.assertGreater(actual, size)
                self.assertLessEqual(actual - size, 16)

    def test_zero_or_invalid_chunk_limits_fail_before_loop(self):
        for limit in [0, -1, False, True, 1.5, "4", None]:
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    split_text("not empty", limit)

    def test_non_string_text_is_rejected(self):
        for text in [b"abc", None, 12, ["a"]]:
            with self.subTest(type=type(text).__name__):
                with self.assertRaises(TypeError):
                    split_text(text)

    def test_negative_or_noninteger_padding_is_rejected(self):
        for size in [-1, False, True, 1.5, "2", None]:
            with self.subTest(size=size):
                with self.assertRaises(ValueError):
                    aes_ecb_padded_size(size)

    def test_default_policy_and_documented_newline_behavior(self):
        self.assertEqual(split_text("a" * 4001), ["a" * 4000, "a"])
        self.assertEqual(split_text("ab\n\ncd", 4), ["ab", "cd"])
        self.assertEqual(split_text(""), [""])
        self.assertEqual(aes_ecb_padded_size(0), 16)
        self.assertEqual(aes_ecb_padded_size(16), 32)


if __name__ == "__main__":
    unittest.main()
