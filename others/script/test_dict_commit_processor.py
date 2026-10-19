#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dict_commit_processor.lua 的运行时单测（不依赖 Rime/Squirrel，用 lupa 内嵌 LuaJIT 执行）

验证内容：
1. Ctrl+D / \\ / Ctrl+Shift+Return 上屏「候选词 + 完整释义」
2. 释义中的 ¤ 学习元数据（音标/★/◆/考试标签/BNC/FRQ）不会泄漏到上屏文本
3. 词性标注（n. / int. / vt. …）与 NBSP、全角分隔符被清洗
4. 不按 max_defs 截断：Ctrl+D 的语义是完整释义
5. 旧格式（不含 ¤）兼容；查不到释义时只上屏候选词
6. Ctrl+E 不被本处理器拦截（让位给 comment_rank_commit / commit_comment）

同时做一条配置契约检查：rime_ice.schema.yaml 的 dict_comment_filter/max_defs
必须 >= 3，否则 Ctrl+T 的第 3 个译词永远不会出现。

运行：python3 others/script/test_dict_commit_processor.py
依赖：pip install lupa（LuaJIT 运行时）；读取 opencc/*.txt 真实产物数据
"""

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
LUA_FILE = os.path.join(ROOT, "lua", "dict_commit_processor.lua")
ECDICT_TXT = os.path.join(ROOT, "opencc", "ecdict.txt")
CEDICT_TXT = os.path.join(ROOT, "opencc", "cedict.txt")
SCHEMA = os.path.join(ROOT, "rime_ice.schema.yaml")
CUSTOM = os.path.join(ROOT, "rime_ice.custom.yaml")

HARNESS = r"""
local lua_path, ECDICT, CEDICT = ...
Opencc = function(name)
    local db = (name:find("ecdict") and ECDICT) or CEDICT
    return { convert_word = function(self, w) return db[w] end }
end
local M = dofile(lua_path)

local state = { text = "", committed = nil, cleared = false }
local env = {
    engine = {
        schema = { config = { get_string = function(self, k) return nil end } },
        context = {
            is_composing = function() return true end,
            has_menu = function() return true end,
            get_selected_candidate = function() return { text = state.text } end,
            clear = function() state.cleared = true end,
        },
        commit_text = function(self, t) state.committed = t end,
    },
}
M.init(env)

local function run(text, repr)
    state.text = text
    state.committed = nil
    state.cleared = false
    local key = {
        release = function() return false end,
        repr = function() return repr end,
    }
    local ret = M.func(key, env)
    return ret, state.committed or "", state.cleared
end

local results = {}
local function emit(name, text, repr)
    local ret, out, cleared = run(text, repr)
    results[#results + 1] = {
        name = name, ret = ret, out = out, cleared = cleared,
    }
end

-- 真实词典样本
emit("apple-CtrlD", "apple", "Control+d")
emit("hello-CtrlD", "hello", "Control+d")
emit("你好-CtrlD", "你好", "Control+d")
emit("chain-反斜杠", "chain", "backslash")
emit("apple-CtrlShiftReturn", "apple", "Control+Shift+Return")
emit("apple-CtrlE不拦截", "apple", "Control+e")
-- 合成：完整释义不按 max_defs 截断
emit("manydefs-CtrlD", "manydefs", "Control+d")
emit("semimeta-CtrlD", "semimeta", "Control+d")
-- 合成：旧格式（无 ¤）+ 中文逗号
emit("oldword-CtrlD", "oldword", "Control+d")
-- 合成：中译英逐项带元数据
emit("测试词-CtrlD", "测试词", "Control+d")
-- 查不到 / 空白
emit("unknown-CtrlD", "unknown", "Control+d")
emit("空白文本", "   ", "Control+d")
-- 候选词首尾空白应被修剪
emit("padded-CtrlD", "  apple  ", "Control+d")

return results
"""


def load_dict(path, keys):
    d = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if "\t" not in line:
                continue
            k, v = line.rstrip("\n").split("\t", 1)
            if k in keys:
                d[k] = v
    return d


def check_max_defs():
    """配置契约：max_defs 必须 >= 3，否则 Ctrl+T 无第 3 个译词可上屏。

    上游 schema 已不再直接修改，max_defs 由 rime_ice.custom.yaml 提供；
    这里同时兼容 schema/custom 两种位置。
    """
    value = None
    source = None
    for path in (CUSTOM, SCHEMA):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"^\s*max_defs:\s*(\d+)", text, re.M)
        if m:
            value = int(m.group(1))
            source = path
            break
    if value is None:
        return ["rime_ice.schema/custom 均未找到 dict_comment_filter/max_defs"]
    if value < 3:
        return ["%s 的 max_defs=%s < 3，Ctrl+T 不可用" % (source, value)]
    return []


def main():
    try:
        import lupa
    except ImportError:
        print("缺少依赖：pip install lupa")
        return 2

    errors = check_max_defs()

    ec = load_dict(ECDICT_TXT, {"apple", "hello", "chain"})
    ec["manydefs"] = "一; 二; 三; 四¤p1¤★¤◆¤中¤100¤200"
    ec["oldword"] = "n.\u00a0旧释义，旧释义2"
    ec["semimeta"] = "n.\u00a0主释义¤p1¤★¤中;高¤100¤200"
    ce = load_dict(CEDICT_TXT, {"你好"})
    ce["测试词"] = (
        "n.\u00a0alpha¤ælfə¤★★★¤◆¤中¤100¤200; "
        "vt.\u00a0beta¤bi:tə¤★★¤◆¤高¤300¤400"
    )

    L = lupa.LuaRuntime()
    E = L.table_from(ec)
    C = L.table_from(ce)
    raw = L.execute(HARNESS, LUA_FILE, E, C)
    items = list(raw.values()) if hasattr(raw, "values") else list(raw)
    results = [(item["name"], int(item["ret"]), item["out"], bool(item["cleared"]))
               for item in items]

    expected = {
        "apple-CtrlD":           (1, "apple (苹果)"),
        "hello-CtrlD":           (1, "hello (你好)"),
        "你好-CtrlD":             (1, "你好 (hello; hi)"),
        "chain-反斜杠":           (1, "chain (链)"),
        "apple-CtrlShiftReturn": (1, "apple (苹果)"),
        "apple-CtrlE不拦截":      (2, ""),
        "manydefs-CtrlD":        (1, "manydefs (一; 二; 三; 四)"),
        "semimeta-CtrlD":        (1, "semimeta (主释义)"),
        "oldword-CtrlD":         (1, "oldword (旧释义; 旧释义2)"),
        "测试词-CtrlD":           (1, "测试词 (alpha; beta)"),
        "unknown-CtrlD":         (1, "unknown"),
        "空白文本":                (2, ""),
        "padded-CtrlD":          (1, "apple (苹果)"),
    }

    ok = bad = 0
    for name, ret, out, cleared in results:
        want = expected.get(name)
        if want is None:
            continue
        wret, wout = want
        good = (ret == wret and out == wout)
        # 任何上屏结果都不允许出现元数据分隔符/星级/牛津标记
        if any(ch in out for ch in ("¤", "★", "◆")):
            good = False
        # 成功上屏应清理上下文；放行则不应清理
        if ret == 1 and not cleared:
            good = False
        if ret == 2 and cleared:
            good = False
        if good:
            print("  PASS %-22s ret=%d out=%r" % (name, ret, out))
            ok += 1
        else:
            print("  FAIL %-22s got=(%d,%r) want=(%d,%r)" % (name, ret, out, wret, wout))
            bad += 1

    for e in errors:
        print("  FAIL 配置契约: %s" % e)
        bad += 1

    print("\n结果：%d 通过，%d 失败" % (ok, bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
