#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""语义等价检测 + 店铺私有活清单 自测 (纯离线, 不碰店铺/不联网)。

运行: python test_compliance_v2.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import active_words
import compliance
import listing_audit


class TestLiteralRegression(unittest.TestCase):
    def test_legacy_fields_intact(self):
        hits = compliance.scan("全网最低价 加微信 好评返现")
        self.assertEqual(len(hits), 3)
        for h in hits:
            for key in ("keyword", "category", "severity", "hint", "start", "end", "matched"):
                self.assertIn(key, h)
            self.assertEqual(h["source"], "literal")
        self.assertEqual(compliance.hit_summary(hits), "通用规则 3")

    def test_no_overlap(self):
        hits = compliance.scan("全网最低价 最好")
        for a, b in zip(hits, hits[1:]):
            self.assertLessEqual(a["end"], b["start"], f"命中重叠: {a} / {b}")

    def test_guard_still_works(self):
        r = compliance.guard("蕞棒的面膜, 加薇信")
        self.assertFalse(r["passed"])
        self.assertNotIn("蕞棒", r["safe_text"])
        self.assertNotIn("薇信", r["safe_text"])


class TestSemanticEquivalence(unittest.TestCase):
    def test_rephrased_words(self):
        cases = [
            "最棒的面膜", "蕞好的面膜", "最 好 用", "頂級配置", "行业第一",
            "全网底价", "立竿见影", "加薇信", "晒图返现", "100%正品", "转运",
        ]
        for text in cases:
            with self.subTest(text=text):
                hits = compliance.scan(text)
                self.assertTrue(any(h["source"] == "semantic" for h in hits),
                                f"{text!r} 未被语义层识别: {hits}")

    def test_normalized_position_maps_to_original(self):
        hits = compliance.scan("最 好")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["matched"], "最 好")
        self.assertEqual(hits[0]["start"], 0)
        self.assertEqual(hits[0]["end"], 3)

    def test_sources_visible_in_audit(self):
        r = listing_audit.audit_text("最棒的面膜 顶级配置", "换皮词")
        self.assertIn("语义等价", r["sources"])
        self.assertTrue(any("语义等价" in x for x in r["hit_labels"]))

    def test_low_risk_beauty_hint(self):
        r = listing_audit.audit_text("美白面膜")
        self.assertEqual(r["level"], "低")  # low 级只提示, 不改变风险等级
        self.assertTrue(any("需特证" in f["category"] for f in r["findings"]))


class TestActiveWords(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "active_banned_words.json"
        self.old_env = os.environ.get(active_words.ENV_KEY)
        os.environ[active_words.ENV_KEY] = str(self.path)
        active_words.clear_cache()

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop(active_words.ENV_KEY, None)
        else:
            os.environ[active_words.ENV_KEY] = self.old_env
        active_words.clear_cache()
        self.tmp.cleanup()

    def _write(self, words):
        self.path.write_text(json.dumps({"version": 1, "words": words}, ensure_ascii=False),
                             encoding="utf-8")

    def test_load_terms_and_aliases(self):
        self._write([
            {"term": "独家秘方", "category": "驳回词", "severity": "high",
             "hint": "独特配方", "aliases": ["独家配方"], "source": "测试驳回"},
            "最牛面膜",
        ])
        hits = compliance.scan("这个独家秘方 独家配方 最牛面膜 普通词")
        active = [h for h in hits if h["source"] == "active"]
        self.assertEqual(len(active), 3)
        self.assertTrue(any(h["keyword"] == "独家配方" and h["hint"] == "独特配方" for h in active))
        self.assertTrue(any(h["keyword"] == "最牛面膜" for h in active))
        self.assertEqual(active_words.stats()["loaded"], 2)

    def test_hot_reload(self):
        self._write(["词甲"])
        self.assertTrue(any(h["keyword"] == "词甲" for h in compliance.scan("词甲")))
        self._write(["词乙"])
        # 确保 mtime 变化 (NTFS 精度有限)
        st = self.path.stat()
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        hits = compliance.scan("词甲 词乙")
        self.assertFalse(any(h["keyword"] == "词甲" and h["source"] == "active" for h in hits))
        self.assertTrue(any(h["keyword"] == "词乙" and h["source"] == "active" for h in hits))

    def test_broken_file_degrades(self):
        self.path.write_text("{坏掉的 json", encoding="utf-8")
        active_words.clear_cache()
        self.assertTrue(active_words.stats()["error"])
        hits = compliance.scan("全网最低价")
        self.assertEqual(hits[0]["source"], "literal")

    def test_append_word(self):
        ok, msg = active_words.append_word("驳回词X", source="测试")
        self.assertTrue(ok, msg)
        self.assertEqual(active_words.stats()["loaded"], 1)
        ok, msg = active_words.append_word("驳回词X")
        self.assertFalse(ok)  # 不重复写入

    def test_create_template(self):
        ok, msg = active_words.create_template(str(self.path))
        self.assertTrue(ok, msg)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["words"], [])
        self.assertIn("_example", data)

    def test_disabled_entry_ignored(self):
        self._write([{"term": "不生效词", "enabled": False}, "生效词"])
        self.assertEqual(active_words.stats()["loaded"], 1)
        self.assertFalse(any(h["keyword"] == "不生效词" for h in compliance.scan("不生效词 生效词")))


class TestPrivacyOfActiveList(unittest.TestCase):
    def test_private_file_is_ignored_by_git(self):
        r = subprocess.run(["git", "check-ignore", "-v", "data/active_banned_words.json"],
                           cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"活清单未被忽略: {r.stdout} {r.stderr}")

    def test_template_is_tracked(self):
        r = subprocess.run(["git", "check-ignore", "assets/active_banned_words.example.json"],
                           cwd=str(ROOT), capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, "示例模板不应被忽略")


if __name__ == "__main__":
    unittest.main(verbosity=2)
