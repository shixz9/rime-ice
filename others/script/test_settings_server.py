#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
settings_server.py 的纯 Python 单测（不启动 HTTP 服务、不写真实配置）

覆盖：
1. import_tables 未变化时，patch_rime_ice_dict 不改 version（避免 97MB 主表重建）
2. 面板不认识的 import_tables 条目（如 cn_dicts/mydict1）被原样保留
3. 词库开关真的变化时才 bump version
4. 主题 "跟随系统" 生成 Squirrel 认识的 native，并同时写 color_scheme_dark

运行：python3 others/script/test_settings_server.py
依赖：无（标准库）
"""

import importlib.util
import os
import pathlib
import re
import tempfile
import unittest
from datetime import datetime

_HERE = pathlib.Path(__file__).resolve().parent
SERVER = _HERE / "settings_server.py"


def load_server():
    spec = importlib.util.spec_from_file_location("rime_settings_server", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PatchDictTests(unittest.TestCase):
    def setUp(self):
        self.ss = load_server()
        self.orig_text = self.ss.F_DICT.read_text(encoding="utf-8")
        self.tmp = tempfile.TemporaryDirectory()
        self.dict_path = pathlib.Path(self.tmp.name) / "rime_ice.dict.yaml"
        self.dict_path.write_text(self.orig_text, encoding="utf-8")
        self.ss.F_DICT = self.dict_path
        self.all_tables = [t for t, _, _ in self.ss.DICT_TABLES]

    def tearDown(self):
        self.tmp.cleanup()

    def test_unchanged_dict_keeps_version(self):
        before = re.search(r'version: "([^"]*)"', self.orig_text).group(1)
        out = self.ss.patch_rime_ice_dict({"dicts_on": self.all_tables})
        self.assertEqual(out, self.orig_text)
        self.assertEqual(re.search(r'version: "([^"]*)"', out).group(1), before)

    def test_unknown_import_is_preserved(self):
        text = self.orig_text.replace(
            "  - cn_dicts/poetry.songci",
            "  - cn_dicts/poetry.songci\n  - cn_dicts/mydict1  # 自建词库")
        self.dict_path.write_text(text, encoding="utf-8")
        out = self.ss.patch_rime_ice_dict({"dicts_on": self.all_tables})
        self.assertIn("  - cn_dicts/mydict1  # 自建词库", out)
        # 第二次保存应稳定，不再重复 bump
        out2 = self.ss.patch_rime_ice_dict({"dicts_on": self.all_tables})
        self.assertEqual(out, out2)

    def test_table_change_bumps_version(self):
        changed = [t for t in self.all_tables if t != "poetry.tang"]
        out = self.ss.patch_rime_ice_dict({"dicts_on": changed})
        self.assertNotIn("cn_dicts/poetry.tang", out)
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(re.search(r'version: "([^"]*)"', out).group(1), today)


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.ss = load_server()
        self.state = self.ss.build_state()
        self.model = self.state["model"]
        self.schema, _ = self.ss.read_json_file(self.ss.F_SCHEMA_SRC)
        self.eng = self.schema.get("engine", {})

    def gen_rime(self):
        return self.ss.gen_rime_ice_custom(
            self.model,
            self.eng.get("processors"),
            self.schema.get("key_binder", {}).get("bindings") or [],
            self.schema.get("switches") or [],
            self.eng.get("segmentors"),
            self.eng.get("translators"),
            self.eng.get("filters"),
        )

    def test_rime_custom_keeps_all_features(self):
        out = self.gen_rime()
        for needle in (
            "switches/@6:", "name: dict_comment",
            "switches/@7:", "dict_comment_phonetic",
            "switches/@8:", "dict_comment_detail",
            "max_defs: 3", "max_length: 60",
            "lua_processor@*dict_commit_processor",
            "lua_processor@*comment_rank_commit",
            "lua_processor@*markdown_processor",
            "affix_segmentor@markdown_mD",
            "lua_translator@*markdown_translator@markdown",
            "lua_filter@*dict_comment_filter",
            'recognizer/patterns/markdown',
            "Control+Shift+Y", "Control+Shift+X",
        ):
            self.assertIn(needle, out)

    def test_default_custom_saves_dict_comment(self):
        out = self.ss.gen_default_custom(self.model)
        self.assertIn("save_options:", out)
        self.assertIn("      - dict_comment", out)

    def test_build_state_meta_has_local_switches(self):
        names = [s.get("name") for s in self.state["switches"]]
        self.assertIn("dict_comment", names)
        self.assertIn("dict_comment_phonetic", names)
        self.assertIn("dict_comment_detail", names)

class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.ss = load_server()
        self.base_style = {
            "color_scheme": "system",
            "layout": "stacked",
            "font_face": "FZZhunYuan-M02",
            "font_point": 20,
            "label_font": True,
            "label_font_point": 20,
            "inline_preedit": True,
            "corner_radius": 6,
        }

    def gen(self, scheme):
        style = dict(self.base_style)
        style["color_scheme"] = scheme
        return self.ss.gen_squirrel_custom({"style": style})

    def test_system_maps_to_native(self):
        out = self.gen("system")
        self.assertIn('color_scheme: "native"', out)
        self.assertIn('color_scheme_dark: "native"', out)
        self.assertNotIn('color_scheme: "system"', out)

    def test_custom_scheme_used_for_both_modes(self):
        out = self.gen("luna")
        self.assertIn('color_scheme: "luna"', out)
        self.assertIn('color_scheme_dark: "luna"', out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
