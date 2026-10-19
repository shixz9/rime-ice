#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Rime 设置面板 · 后端
# ---------------------------------------------------------------------------
# 为首页 settings_panel.html 提供本地 API：
#   GET  /            → 面板页面
#   GET  /api/state   → 当前配置（解析后的设置模型 + 原始 YAML 文本）
#   POST /api/save    → 按模型重写 *.custom.yaml / rime_ice.dict.yaml
#   POST /api/deploy  → 触发 Squirrel --reload 并轮询等待后台重建完成
#   POST /api/check   → 运行 others/script/check_custom_patch.rb 校验漂移
#   POST /api/restore → 从 .settings_backup/<ts>/ 恢复备份
#
# 只监听 127.0.0.1；写入前自动备份到 others/script/.settings_backup/<时间戳>/。
# 仅依赖 Python 标准库；YAML 解析借系统 ruby(psych)。
# ---------------------------------------------------------------------------
import json, os, re, shutil, subprocess, sys, tempfile, threading, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

RIME_DIR   = Path(__file__).resolve().parent.parent.parent   # ~/Library/Rime（others/script/ 的上两级）
SCRIPT_DIR = RIME_DIR / "others" / "script"
BACKUP_DIR = SCRIPT_DIR / ".settings_backup"
BUILD_DIR  = RIME_DIR / "build"                # 重建产物目录（轮询部署完成用）
PANEL_HTML = SCRIPT_DIR / "settings_panel.html"
SQUIRREL   = "/Library/Input Methods/Squirrel.app/Contents/MacOS/Squirrel"
# 页面心跳：面板页面全部关闭超过该秒数后，服务自动退出（随用随开，不常驻）
PAGE_IDLE_MAX = 90
_page_lock = threading.Lock()
_page_state = {"ever": False, "last": 0.0}
_inflight   = {"n": 0}          # 部署/检查进行中，豁免自动退出

F_DEFAULT = RIME_DIR / "default.custom.yaml"
F_SQUIRREL = RIME_DIR / "squirrel.custom.yaml"
F_SCHEMA  = RIME_DIR / "rime_ice.custom.yaml"
F_DICT    = RIME_DIR / "rime_ice.dict.yaml"
F_BASE_DEF  = RIME_DIR / "default.yaml"          # 上游基线（只读参照）
F_BASE_STY  = RIME_DIR / "squirrel.yaml"         # 上游样式基线
F_SCHEMA_SRC = RIME_DIR / "rime_ice.schema.yaml" # 上游方案（processors/switches）

WRITABLE = [F_DEFAULT, F_SQUIRREL, F_SCHEMA, F_DICT]

# 面板可管的方案（顺序即 schema_list 顺序；t9 已被上游删除，见 default.custom.yaml 注释）
SCHEMAS = [
    ("rime_ice",              "雾凇拼音（全拼）"),
    ("double_pinyin",         "自然码双拼"),
    ("double_pinyin_abc",     "智能 ABC 双拼"),
    ("double_pinyin_mspy",    "微软双拼"),
    ("double_pinyin_sogou",   "搜狗双拼"),
    ("double_pinyin_flypy",   "小鹤双拼"),
    ("double_pinyin_ziguang", "紫光双拼"),
    ("double_pinyin_jiajia",  "拼音加加双拼"),
]
# key_binder 分组：每组对应 default.yaml 中「一段」生效绑定，可整组开关
KEY_GROUPS = [
    ("cursor",    "光标移动",   "Tab / Shift+Tab / Alt+←→ 在拼音间移动光标"),
    ("paging_me", "翻页 - =",   "minus → 上一页，equal → 下一页"),
    ("paging_cp", "翻页 , .",   "comma → 上一页，period → 下一页（仅在候选菜单出现时）"),
    ("punct",     "中英标点 / 简繁", "Ctrl+Shift+3 中英标点，Ctrl+Shift+4 简繁切换"),
    ("keypad",    "小键盘映射", "小键盘 0-9 . + - * / 回车 映射到主键盘"),
]
# rime_ice.dict.yaml 可挂载的词库（顺序即 import_tables 顺序）
DICT_TABLES = [
    ("8105",           "8105 常用字表",              True),
    ("41448",          "41448 大字表（生僻字）",      False),
    ("base",           "基础词库",                   True),
    ("ext",            "扩展词库",                   True),
    ("tencent",        "腾讯词向量 · 约 98 万条（部署最慢的一项）", True),
    ("others",         "杂项词库",                   True),
    ("poetry.classics","古诗词 · 经典（诗经/论语/楚辞/蒙学，权重 8）", True),
    ("poetry.tang",    "全唐诗 + 补录（权重 5）",     True),
    ("poetry.songci",  "全宋词（权重 5）",            True),
]

# ────────────────────────── YAML 解析（借 ruby） ──────────────────────────

def yaml_to_json(text):
    """用系统 ruby(psych) 把 YAML 文本转 JSON 字符串；失败返回 None。"""
    try:
        p = subprocess.run(
            ["ruby", "-ryaml", "-rjson", "-e",
             "print(JSON.generate(YAML.load(STDIN.read, aliases: true)))"],
            input=text, capture_output=True, text=True, timeout=10)
        return p.stdout if p.returncode == 0 and p.stdout.strip() else None
    except Exception:
        return None

def read_json_file(path):
    raw = Path(path).read_text(encoding="utf-8")
    js = yaml_to_json(raw)
    return (json.loads(js) if js else {}), raw

def deep_merge(base, patch):
    """Rime 的 patch 合并语义：dict 递归，其余整体覆盖。"""
    out = dict(base)
    for k, v in (patch or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out

# ────────────────────────── 模型读取 ──────────────────────────

def ctrl_e_state(e_custom):
    patch = e_custom.get("patch") or {}
    keys = patch.keys() if isinstance(patch, dict) else []
    has = any(isinstance(k, str) and k.endswith("Control+e") for k in keys)
    return patch.get("editor/bindings/Control+e", "commit_comment") if has else "off"

def switch_state(e_custom, name, default=1):
    """从 rime_ice.custom.yaml 补丁中读取指定名字 switch 的 reset 值（0/1）"""
    patch = e_custom.get("patch") or {}
    if not isinstance(patch, dict):
        return default
    for k, v in patch.items():
        if isinstance(k, str) and k.startswith("switches/@") and isinstance(v, dict) \
                and v.get("name") == name:
            try:
                return 1 if int(v.get("reset", default)) == 1 else 0
            except (TypeError, ValueError):
                return default
    return default

def build_state():
    d_custom, d_raw = read_json_file(F_DEFAULT)
    s_custom, s_raw = read_json_file(F_SQUIRREL)
    e_custom, e_raw = read_json_file(F_SCHEMA)
    dict_custom, dict_raw = read_json_file(F_DICT)
    base_def, _  = read_json_file(F_BASE_DEF)
    base_sty, _  = read_json_file(F_BASE_STY)
    schema_src, _ = read_json_file(F_SCHEMA_SRC)

    eff_def = deep_merge(base_def, d_custom.get("patch", {}))
    eff_sty = deep_merge(base_sty.get("style", {}), s_custom.get("patch", {}).get("style", {}))

    model = {
        "page_size":      eff_def.get("menu", {}).get("page_size", 5),
        "schemas_on":     [s.get("schema") for s in
                           (d_custom.get("patch", {}).get("schema_list") or [])],
        "switcher_hotkeys": bool(d_custom.get("patch", {}).get("switcher", {}).get("hotkeys")),
        "ascii": {
            "caps":    bool(d_custom.get("patch", {}).get("ascii_composer", {}).get("good_old_caps_lock", True)),
            "shift_l": eff_def.get("ascii_composer", {}).get("switch_key", {}).get("Shift_L", "commit_code"),
            "shift_r": eff_def.get("ascii_composer", {}).get("switch_key", {}).get("Shift_R", "noop"),
        },
        "style": {
            "layout":          eff_sty.get("candidate_list_layout", "stacked"),
            "font_face":       eff_sty.get("font_face", "Avenir"),
            "font_point":      eff_sty.get("font_point", 16),
            "label_font":      "label_font_face" in s_custom.get("patch", {}).get("style", {}),
            "label_font_point":eff_sty.get("label_font_point", 20),
            "corner_radius":   eff_sty.get("corner_radius", 7),
            "inline_preedit":  bool(eff_sty.get("inline_preedit", True)),
            "color_scheme":    ("system" if eff_sty.get("color_scheme") == "native"
                                else eff_sty.get("color_scheme", "purity_of_form_custom")),
        },
        "ctrl_e":       ctrl_e_state(e_custom),
        "rank_commit":  any(p == "lua_processor@*comment_rank_commit"
                            for p in (e_custom.get("patch", {}).get("engine/processors") or [])),
        "comment_phonetic": bool(switch_state(e_custom, "dict_comment_phonetic", 1)),
        "comment_detail":   bool(switch_state(e_custom, "dict_comment_detail", 0)),
        "dicts_on":     [t.split("#")[0].strip().replace("cn_dicts/", "", 1) for t in
                         (dict_custom.get("import_tables") or [])],
    }
    # key_binder 分组：按 accept 键推断每组是否开启（以当前 custom 文件为准，
    # 缺省回落到上游 default.yaml 的生效绑定）
    bindings_now = (d_custom.get("patch", {}).get("key_binder", {}).get("bindings")
                    or base_def.get("key_binder", {}).get("bindings") or [])
    binds_now = {b.get("accept") for b in bindings_now if isinstance(b, dict)}
    groups = {
        "cursor":    {"Shift+Tab": True, "Tab": True, "Alt+Left": True, "Alt+Right": True},
        "paging_me": {"minus": True, "equal": True},
        "paging_cp": {"comma": True, "period": True},
        "punct":     {"Control+Shift+3": True, "Control+Shift+4": True},
        "keypad":    {"KP_Add": True},
    }
    model["keys"] = {g: all(k in binds_now for k in ks) for g, ks in groups.items()}
    model["select_char"] = "select_first_character" in d_custom.get("patch", {}).get("key_binder", {}) \
                           or "select_first_character" in base_def.get("key_binder", {})

    return {
        "model": model,
        "schemas": [{"id": i, "name": n} for i, n in SCHEMAS],
        "key_groups": [{"id": g, "name": n, "desc": d} for g, n, d in KEY_GROUPS],
        "dicts": [{"id": i, "name": n, "default_on": d} for i, n, d in DICT_TABLES],
        "switches": schema_src.get("switches", []),
        "upstream_processors": schema_src.get("engine", {}).get("processors", []),
        "raw": {"default": d_raw, "squirrel": s_raw,
                "schema": e_raw, "dict": dict_raw},
    }

# ────────────────────────── 文件生成 ──────────────────────────

def _bindings(m):
    """按模型重建 key_binder.bindings（整体替换，顺序固定）。"""
    ks = m ["keys"]
    out = []
    if ks.get("cursor", True):
        out += [
            {"when": "composing", "accept": "Shift+Tab", "send": "Shift+Left"},
            {"when": "composing", "accept": "Tab",       "send": "Shift+Right"},
            {"when": "composing", "accept": "Alt+Left",  "send": "Shift+Left"},
            {"when": "composing", "accept": "Alt+Right", "send": "Shift+Right"},
        ]
    if ks.get("paging_me", True):
        out += [
            {"when": "has_menu", "accept": "minus",  "send": "Page_Up"},
            {"when": "has_menu", "accept": "equal",  "send": "Page_Down"},
        ]
    if ks.get("punct", True):
        out += [
            {"when": "always", "toggle": "ascii_punct",        "accept": "Control+Shift+3"},
            {"when": "always", "toggle": "ascii_punct",        "accept": "Control+Shift+numbersign"},
            {"when": "always", "toggle": "traditionalization", "accept": "Control+Shift+4"},
            {"when": "always", "toggle": "traditionalization", "accept": "Control+Shift+dollar"},
        ]
    if ks.get("keypad", True):
        for i in range(10):
            out.append({"accept": f"KP_{i}", "send": str(i), "when": "composing"})
        out += [
            {"accept": "KP_Decimal",  "send": "period",   "when": "composing"},
            {"accept": "KP_Multiply", "send": "asterisk", "when": "composing"},
            {"accept": "KP_Add",      "send": "plus",     "when": "composing"},
            {"accept": "KP_Subtract", "send": "minus",    "when": "composing"},
            {"accept": "KP_Divide",   "send": "slash",    "when": "composing"},
            {"accept": "KP_Enter",    "send": "Return",   "when": "composing"},
        ]
    if ks.get("paging_cp", True):
        out += [
            {"when": "has_menu", "accept": "comma",  "send": "Page_Up"},
            {"when": "has_menu", "accept": "period", "send": "Page_Down"},
        ]
    return out

def _y(v):
    """标量 YAML 序列化：数字/布尔原样，其余加双引号。"""
    if isinstance(v, bool):  return "true" if v else "false"
    if isinstance(v, (int, float)): return str(v)
    return '"' + str(v).replace('\\', '\\\\').replace('"', '\\"') + '"'

def _flow(b):
    parts = []
    for k in ("when", "accept", "toggle", "send", "select"):
        if k in b:
            parts.append(f"{k}: {_y(b[k])}")
    return "    - { " + ", ".join(parts) + " }"

def gen_default_custom(m):
    L = []
    L.append("# 输入行为设置（自定义补丁）")
    L.append("# 由「Rime 设置面板」生成于 %s；手动修改会被下次面板保存覆盖" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    L.append("# 注意：不要直接改 default.yaml —— 那是上游仓库文件，git pull 会被覆盖；一律写在这里")
    L.append("# 上游校验：ruby others/script/check_custom_patch.rb")
    L.append("patch:")
    L.append("  # 方案列表：Rime 对列表为整体替换，须完整重列；t9 已随其方案文件删除而去除")
    L.append("  schema_list:")
    for sid, _ in SCHEMAS:
        mark = "" if sid in m["schemas_on"] else "    # 已停用"
        L.append(f"    - schema: {sid}{mark}")
    L.append("")
    L.append("  menu:")
    L.append("    page_size: %d        # 每页候选词数量" % int(m["page_size"]))
    L.append("")
    if m.get("switcher_hotkeys", True):
        L.append("  switcher:")
        L.append("    hotkeys:")
        L.append('      - "Control+grave"   # Ctrl+` 切换输入方案')
        L.append('      - "F4"')
        L.append("")
    L.append("  ascii_composer:")
    L.append("    good_old_caps_lock: %s   # Caps Lock 切换中英" %
             ("true" if m["ascii"].get("caps", True) else "false"))
    L.append("    switch_key:")
    L.append("      Shift_L: %-13s # 左 Shift：%s" % (
        m["ascii"].get("shift_l", "commit_code"),
        {"commit_code": "上屏拼音编码转英文", "commit_text": "上屏候选文字转英文",
         "inline_ascii": "临时英文，回车回中文", "clear": "清除未上屏转英文",
         "noop": "不切换", "set_ascii_mode": "切英文模式",
         "unset_ascii_mode": "切中文模式"}.get(m["ascii"].get("shift_l"), "")))
    L.append("      Shift_R: %-13s # 右 Shift：%s" % (
        m["ascii"].get("shift_r", "noop"),
        {"commit_code": "上屏拼音编码转英文", "commit_text": "上屏候选文字转英文",
         "inline_ascii": "临时英文，回车回中文", "clear": "清除未上屏转英文",
         "noop": "不切换", "set_ascii_mode": "切英文模式",
         "unset_ascii_mode": "切中文模式"}.get(m["ascii"].get("shift_r"), "")))
    L.append("")
    L.append("  # ⚠️ Rime 对列表是整体替换不是追加，以下包含上游默认绑定 + 自定义；")
    L.append("  #    上游 key_binder 变更时执行 check_custom_patch.rb 对照同步")
    L.append("  key_binder:")
    if m.get("select_char", True):
        L.append('    select_first_character: "bracketleft"    # 以词定字：取首字')
        L.append('    select_last_character: "bracketright"    # 以词定字：取尾字')
    L.append("    bindings:")
    for b in _bindings(m):
        L.append(_flow(b))
    L.append("")
    L.append("  # 注：editor 绑定属方案级配置（Ctrl+E 等），改 rime_ice.custom.yaml。")
    L.append("")
    L.append("  # ── 与上游的已知分歧（2026-10-17 逐项核对，均决定保留本地行为）──────")
    L.append("  # 1. switcher/save_options 含 dict_comment（上游 da1fbe6 已移除）：")
    L.append("  #    差异仅在「词典释义开关是否跨会话记忆」——本地=记忆；上游=不记忆，")
    L.append("  #    回落到 rime_ice.schema.yaml 的 reset: 1（默认开）。本地用")
    L.append("  #    Ctrl+Shift+E 频繁切换该开关，保留记忆更顺手，暂不向上游对齐。")
    L.append("  # 2. key_binder /（rime_ice.custom.yaml 的）processors 为整体替换列表，")
    L.append("  #    上游新增的绑定/处理器不会自动继承。每次 git pull 更新后执行：")
    L.append("  #      ruby others/script/check_custom_patch.rb")
    L.append("  #    出现 DRIFT 时，把上游新增项补进对应 custom 文件，再重新部署。")
    L.append("")
    return "\n".join(L)

def gen_squirrel_custom(m):
    st = m["style"]
    scheme = st.get("color_scheme") or "system"
    # Squirrel 的系统主题名是 native；system 只是面板内部的 UI 值。
    if scheme in ("system", "native"):
        scheme = "native"
    L = []
    L.append("# 鼠须管界面设置（自定义补丁，优先级高于 squirrel.yaml）")
    L.append("# 由「Rime 设置面板」生成于 %s；手动修改会被下次面板保存覆盖" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    L.append("# 改完在菜单栏鼠须管图标 →「重新部署」生效（面板上可直接点）")
    L.append("patch:")
    L.append("  style:")
    L.append("    color_scheme: %-28s # 主题（跟随系统用 native）" % _y(scheme))
    L.append("    color_scheme_dark: %-23s # 暗色主题（与亮色同方案）" % _y(scheme))
    L.append("    candidate_list_layout: %-16s # 候选词竖排（linear 为横排）" %
             _y(st.get("layout", "stacked")))
    L.append("    text_orientation: horizontal")
    L.append("    font_face: %-30s # 候选字体" % _y(st.get("font_face", "FZZhunYuan-M02")))
    L.append("    font_point: %-24s # 候选字号（键名是 font_point，不是 font_size）" %
             _y(int(st.get("font_point", 24))))
    if st.get("label_font", True):
        L.append("    label_font_face: %-27s # 候选编号字体" % _y(st.get("font_face", "FZZhunYuan-M02")))
        L.append("    label_font_point: %-24s # 候选编号字号" % _y(int(st.get("label_font_point", 20))))
    L.append("    inline_preedit: %-21s # 拼音串内嵌在输入光标处" %
             ("true" if st.get("inline_preedit", True) else "false"))
    L.append("    corner_radius: %-20s # 候选窗圆角" % _y(int(st.get("corner_radius", 6))))
    L.append("")
    return "\n".join(L)

def gen_rime_ice_custom(m, upstream_processors, upstream_bindings=None, upstream_switches=None):
    L = []
    L.append("# 雾凇拼音方案级补丁（rime_ice.custom.yaml）")
    L.append("# 由「Rime 设置面板」生成于 %s；手动修改会被下次面板保存覆盖" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    L.append("# 方案文件的 custom 补丁：对本方案的 engine/editor 等做修改")
    L.append("patch:")

    # 学习元数据档位开关（P1 富化的显示控制）
    # 说明：librime 的 context:get_option 仅返回布尔值，多状态开关索引不可读，
    # 故用两个布尔开关组合三档：均关=极简 / phonetic=标准(+音标◆) / detail=完整(+星级标签词频)。
    # 追加索引基于上游列表当前长度；上游新增项后需顺延（check_custom_patch.rb 会校验）。
    n_sw = len(upstream_switches) if upstream_switches else 7
    L.append("  # 英语学习元数据档位（详见 lua/dict_comment_filter.lua 头注释）")
    L.append("  switches/@%d:" % n_sw)
    L.append("    name: dict_comment_phonetic")
    L.append("    states: [ 无音标, 音标 ]")
    L.append("    reset: %d" % (1 if m.get("comment_phonetic", True) else 0))
    L.append("  switches/@%d:" % (n_sw + 1))
    L.append("    name: dict_comment_detail")
    L.append("    states: [ 无考点, 考点详注 ]")
    L.append("    reset: %d" % (1 if m.get("comment_detail", False) else 0))
    L.append("")

    # 档位切换快捷键（Y=音 yīn 标档，X=详 xiáng 注档；F4 方案菜单亦可切换）
    n_bd = len(upstream_bindings) if upstream_bindings else 5
    L.append("  # 学习档位切换快捷键")
    L.append("  key_binder/bindings/@%d:" % n_bd)
    L.append('    { when: always, accept: "Control+Shift+Y", toggle: dict_comment_phonetic }')
    L.append("  key_binder/bindings/@%d:" % (n_bd + 1))
    L.append('    { when: always, accept: "Control+Shift+X", toggle: dict_comment_detail }')
    L.append("")

    # 释义长度兜底 45→60：学习元数据（音标/星级/标签/词频）需要展示空间
    L.append("  # 释义最大字符数：45 → 60，为学习元数据留空间（超出仍截断加 …）")
    L.append("  dict_comment_filter/max_length: 60")
    L.append("")

    if m.get("ctrl_e", "off") != "off":
        action = {"commit_text":  "候选 + 释义一起上屏（原版行为）",
                  "commit_comment": "只上屏释义，不输出中文词"}.get(m["ctrl_e"], m["ctrl_e"])
        L.append("  # Ctrl+E：%s" % action)
        L.append('  "editor/bindings/Control+e": %s' % m["ctrl_e"])
        L.append("")
    L.append("  # 处理器链：列表是整体替换，以下为 rime_ice.schema.yaml 原列表 + 面板新增项")
    L.append("  engine/processors:")
    procs = list(upstream_processors) or [
        "lua_processor@*select_character", "lua_processor@*dict_commit_processor",
        "lua_processor@*markdown_processor", "ascii_composer", "recognizer",
        "key_binder", "speller", "punctuator", "selector", "navigator", "express_editor"]
    for p in procs:
        L.append("    - %s" % p)
        if p.endswith("dict_commit_processor") and m.get("rank_commit"):
            L.append("    - lua_processor@*comment_rank_commit   # 新增：中文候选英文注释 Ctrl+E/R 按顺位上屏")
    L.append("")
    return "\n".join(L)

def patch_rime_ice_dict(m):
    """外科手术式改写 import_tables 段。

    两个约束：
    1. 只有 import_tables 真的变化时才 bump version；否则原样返回，
       避免改字体/快捷键也触发 97MB 主词库重建。
    2. 面板不认识的 import 行（例如手写的 cn_dicts/mydict1）原样保留，
       不能被面板保存静默删掉。
    """
    text = F_DICT.read_text(encoding="utf-8")
    on = set(m.get("dicts_on") or [])
    comments = {
        "8105": "字表", "41448": "大字表（按需启用）（启用时和 8105 同时启用并放在 8105 下面）",
        "base": "基础词库", "ext": "扩展词库",
        "tencent": "腾讯词向量（大词库，部署时间较长）", "others": "一些杂项",
        "poetry.classics": "诗经/论语/楚辞/五代词/蒙学（权重 8）",
        "poetry.tang": "全唐诗 + 补录（权重 5）", "poetry.songci": "全宋词（权重 5）",
    }
    order = [i for i, _, _ in DICT_TABLES]
    known = set(order)

    block_re = re.compile(r"import_tables:\n(?:[ \t]+.*\n|\n)*")
    match = block_re.search(text)
    if not match:
        raise RuntimeError("未找到 import_tables 段，拒绝写入")
    old_block = match.group(0)

    # 保留面板未识别的条目（手写词库、第三方词库等），按原行保留注释
    preserved = []
    seen = set()
    for line in old_block.splitlines()[1:]:
        em = re.match(r"\s*-\s+(\S+)(.*)$", line)
        if not em:
            continue
        path = em.group(1)
        tid = path[len("cn_dicts/"):] if path.startswith("cn_dicts/") else path
        if tid in known:
            continue
        if path in seen:
            continue
        seen.add(path)
        preserved.append(line.rstrip())

    lines = ["import_tables:"]
    for tid in order:
        if tid in on:
            lines.append("  - cn_dicts/%s%s" % (tid, ("  # " + comments[tid]) if tid in comments else ""))
    if preserved:
        lines += ["", "  # 面板未识别的自定义词库，原样保留"]
        lines += preserved
    lines += [
        "",
        "  # 建议把扩展词库放到下面，有重复词条时，最上面的权重生效",
        "  # - mydict1           # 挂载配置目录下的 mydict1.dict.yaml 词库文件",
        "  # - cn_dicts/mydict2  # 挂载 cn_dicts 目录里的 mydict2.dict.yaml 词库文件",
        "",
    ]
    new_block = "\n".join(lines)

    # 没有实际变化：连 version 都不动，避免无谓的词典重建
    if new_block == old_block:
        return text

    text2 = text[:match.start()] + new_block + text[match.end():]
    today = datetime.now().strftime("%Y-%m-%d")
    text2 = re.sub(r'version: "[^"]*"', 'version: "%s"' % today, text2, count=1)
    return text2

# ────────────────────────── HTTP 服务 ──────────────────────────

def deep_equal(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(deep_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(deep_equal(x, y) for x, y in zip(a, b))
    return a == b

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默默认访问日志
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            html = PANEL_HTML.read_text(encoding="utf-8")
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/heartbeat":
            _touch_page()
            self._json({"ok": True})
        elif u.path == "/api/state":
            try:
                self._json(build_state())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif u.path == "/api/backups":
            items = []
            if BACKUP_DIR.is_dir():
                for d in sorted(BACKUP_DIR.iterdir(), reverse=True)[:20]:
                    if d.is_dir():
                        items.append(d.name)
            self._json({"backups": items})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/save":
                self._save()
            elif u.path == "/api/deploy":
                self._deploy()
            elif u.path == "/api/check":
                self._check()
            elif u.path == "/api/restore":
                self._restore()
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    # ── 保存 ─────────────────────────────────────────────────
    def _save(self):
        m = self._body()
        # 防呆：全拼主方案不可停用，否则 Rime 无输入方案可用
        m.setdefault("schemas_on", [])
        if "rime_ice" not in m["schemas_on"]:
            m["schemas_on"].append("rime_ice")

        schema_src, _ = read_json_file(F_SCHEMA_SRC)
        new_files = {
            F_DEFAULT:   gen_default_custom(m),
            F_SQUIRREL:  gen_squirrel_custom(m),
            F_SCHEMA:    gen_rime_ice_custom(
                m,
                schema_src.get("engine", {}).get("processors"),
                schema_src.get("key_binder", {}).get("bindings") or [],
                schema_src.get("switches") or []),
            F_DICT:      patch_rime_ice_dict(m),
        }
        # 写文件（先备份，再原子替换；内容未变的文件不重写，避免无谓 mtime 变化）
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        bdir = BACKUP_DIR / ts
        bdir.mkdir(parents=True, exist_ok=True)
        for p in WRITABLE:
            shutil.copy2(p, bdir / p.name)
        written = []
        for p, text in new_files.items():
            try:
                if p.read_text(encoding="utf-8") == text:
                    continue
            except OSError:
                pass
            fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".panel_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, p)
            written.append(p.name)
        # 自校验：重新读取生成后的配置
        try:
            st1 = build_state()
            errs = self._verify(st1, m)
        except Exception as e:
            errs = ["自校验解析失败：%s" % e]
        if errs:
            # 回滚
            for p in WRITABLE:
                shutil.copy2(bdir / p.name, p)
            return self._json({"ok": False, "errors": errs, "rolled_back": True,
                               "backup": ts})
        self._json({"ok": True, "backup": ts,
                    "files": [p.name for p in new_files],
                    "written": written})

    @staticmethod
    def _verify(st, m):
        errs = []
        got = st["model"]
        if int(got["page_size"]) != int(m["page_size"]):
            errs.append("page_size 应为 %s，实为 %s" % (m["page_size"], got["page_size"]))
        for sid, _ in SCHEMAS:
            if (sid in m.get("schemas_on", [])) != (sid in got["schemas_on"]):
                errs.append("方案 %s 开关不符" % sid)
        for g, _, _ in KEY_GROUPS:
            if bool(m.get("keys", {}).get(g, True)) != bool(got["keys"].get(g)):
                errs.append("按键组 %s 开关不符" % g)
        st = m.get("style", {})
        if int(got["style"]["font_point"]) != int(st.get("font_point", 24)):
            errs.append("字号不符")
        if got["style"]["layout"] != st.get("layout"):
            errs.append("候选布局不符")
        if bool(got.get("rank_commit")) != bool(m.get("rank_commit")):
            errs.append("Ctrl+E/R 顺位上屏开关不符")
        if bool(got.get("comment_phonetic")) != bool(m.get("comment_phonetic", True)):
            errs.append("学习元数据音标档开关不符")
        if bool(got.get("comment_detail")) != bool(m.get("comment_detail", False)):
            errs.append("学习元数据详注档开关不符")
        if got.get("ctrl_e") != m.get("ctrl_e"):
            errs.append("Ctrl+E 行为不符")
        for tid, _, _ in DICT_TABLES:
            if (tid in m.get("dicts_on", [])) != (tid in got["dicts_on"]):
                errs.append("词库 %s 开关不符" % tid)
        return errs

    # ── 部署 ─────────────────────────────────────────────────
    def _deploy(self):
        _inflight["n"] += 1          # 部署期间页面可关，服务不退出
        try:
            self._deploy_inner()
        finally:
            _inflight["n"] -= 1

    def _deploy_inner(self):
        """触发 Squirrel --reload，并轮询等待后台重建真实完成。

        --reload 只向运行中的 Squirrel 发「重新部署」通知：命令毫秒级返回
        rc=0，真正的词库编译在输入法进程里跑（主表约 97MB，实测 1-4 分钟）；
        若上一次重建仍在进行（如连点两次「保存并重新部署」），--reload 会
        直接 rc=1——这不是真的失败。因此这里不以命令返回值下结论，
        而是轮询 build/ 产物：
          产物 mtime 前移        → 正在重建
          连续 3 次（6 秒）无写入 → 重建完成（state=rebuilt）
          约 20 秒窗口内无变化   → 配置无变更、无需重建（state=uptodate）
        """
        def stamp():
            try:
                return {p.name: p.stat().st_mtime_ns for p in BUILD_DIR.glob("*.bin")}
            except Exception:
                return {}

        t0 = time.time()
        pre = stamp()
        pre_max = max(pre.values()) if pre else 0
        try:
            p = subprocess.run([SQUIRREL, "--reload"], capture_output=True,
                               text=True, timeout=60, cwd=str(RIME_DIR))
            rc, out = p.returncode, (p.stdout + p.stderr).strip()[-400:]
        except subprocess.TimeoutExpired:
            return self._json({"ok": False, "state": "hung",
                               "error": "Squirrel --reload 60 秒无响应"})
        except Exception as e:
            return self._json({"ok": False, "state": "error",
                               "error": "调用 Squirrel 失败：%s" % e})

        changed, stable, saw = pre_max, 0, False
        while time.time() - t0 < 240:
            time.sleep(2)
            now = stamp()
            if not now:
                continue
            cur_max = max(now.values())
            if cur_max > changed:
                changed, stable, saw = cur_max, 0, True
            elif saw:
                stable += 1
                if stable >= 3:      # 连续 6 秒无新写入 → 重建完成
                    return self._json({"ok": True, "state": "rebuilt",
                                       "seconds": round(time.time() - t0, 1)})
            elif time.time() - t0 > 20 and rc == 0:
                return self._json({"ok": True, "state": "uptodate",
                                   "seconds": round(time.time() - t0, 1)})
        if saw:
            return self._json({"ok": False, "state": "timeout",
                               "error": "重建已开始但 240 秒仍未完成（主表较大时可能仍在进行，"
                                        "稍后可再点「立即部署」确认）",
                               "seconds": round(time.time() - t0, 1)})
        return self._json({"ok": rc == 0, "state": "nochange", "rc": rc,
                           "seconds": round(time.time() - t0, 1), "out": out,
                           "error": None if rc == 0 else
                                    "Squirrel 返回 rc=%d 且未观察到重建（可能正忙）" % rc})

    # ── 检查 ─────────────────────────────────────────────────
    def _check(self):
        _inflight["n"] += 1
        try:
            p = subprocess.run(["ruby", str(SCRIPT_DIR / "check_custom_patch.rb")],
                               capture_output=True, text=True, timeout=30, cwd=str(RIME_DIR))
            self._json({"ok": p.returncode == 0,
                        "out": (p.stdout + p.stderr)[-2000:]})
        finally:
            _inflight["n"] -= 1

    # ── 恢复 ─────────────────────────────────────────────────
    def _restore(self):
        name = (self._body() or {}).get("backup", "")
        if not re.fullmatch(r"[\d\-]{8,}", name):
            return self._json({"ok": False, "error": "非法备份名"})
        bdir = BACKUP_DIR / name
        if not bdir.is_dir():
            return self._json({"ok": False, "error": "备份不存在"})
        # 恢复前再备份当前状态
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        cur = BACKUP_DIR / ("prerestore-" + ts)
        cur.mkdir(parents=True, exist_ok=True)
        for p in WRITABLE:
            if p.exists():
                shutil.copy2(p, cur / p.name)
        n = 0
        for p in WRITABLE:
            src = bdir / p.name
            if src.exists():
                shutil.copy2(src, p)
                n += 1
        self._json({"ok": True, "restored": n, "prerestore_backup": cur.name})


def _touch_page():
    with _page_lock:
        _page_state["ever"] = True
        _page_state["last"] = time.time()


def _watchdog():
    """无页面访问时自动退出：面板只在被使用时运行。

    判定条件：曾经收到过页面心跳，且距上次心跳超过 PAGE_IDLE_MAX 秒，
    且无部署/检查进行中。从未有人打开过页面则一直等待
    （用户可能稍后才在浏览器里打开地址）。
    """
    while True:
        time.sleep(10)
        with _page_lock:
            ever, last = _page_state["ever"], _page_state["last"]
        if not ever:
            continue
        if _inflight["n"] > 0:
            continue
        if time.time() - last > PAGE_IDLE_MAX:
            break
    os._exit(0)


def main():
    port = 8877
    for try_port in range(8877, 8890):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", try_port), Handler)
            port = try_port
            break
        except OSError:
            continue
    else:
        sys.exit("端口 8877-8889 都被占用")
    url = "http://127.0.0.1:%d/" % port
    print("Rime 设置面板 → %s" % url)
    print("配置目录：%s" % RIME_DIR)
    print("页面全部关闭约 %d 秒后服务自动退出；关闭此窗口立即停止。" % PAGE_IDLE_MAX)
    threading.Thread(target=_watchdog, daemon=True).start()
    try:
        import webbrowser
        if not os.environ.get("RIME_PANEL_NO_BROWSER"):
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
