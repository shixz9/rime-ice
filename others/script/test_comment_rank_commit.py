#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
comment_rank_commit.lua 的运行时单测（不依赖 Rime/Squirrel，用 lupa 内嵌 LuaJIT 执行）

验证内容：
1. Ctrl+E/R/T 分别上屏第 1/2/3 个英文译词
2. 注释不足 N 段时：Ctrl+R/T 吞键（kAccepted，避免穿透宿主应用），
   Ctrl+E 的单条注释仍放行给 editor/commit_comment
3. 候选无注释时吞键（kAccepted，不输出）
4. 非目标按键 / 释放事件 / 未合成 / 无菜单 均放行
5. 尾部省略号与首尾空白的清理

运行：python3 others/script/test_comment_rank_commit.py
依赖：pip install lupa（LuaJIT 运行时）
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
LUA_FILE = os.path.join(_HERE, "..", "..", "lua", "comment_rank_commit.lua")

HARNESS = r"""
local lua_path = ...
if rawget(_G, "yield") == nil then rawset(_G, "yield", coroutine.yield) end
local M = dofile(lua_path)

local committed = nil

local function make_key(repr, is_release)
    return {
        release = function(self) return is_release end,
        repr = function(self) return repr end,
    }
end

local function make_env(comment, composing, has_menu)
    committed = nil
    return {
        engine = {
            commit_text = function(self, t) committed = t end,
            context = {
                is_composing = function(self) return composing end,
                has_menu = function(self) return has_menu end,
                get_selected_candidate = function(self) return { comment = comment } end,
                clear = function(self) end,
            },
        },
    }
end

local function run(repr, comment, composing, has_menu, is_release)
    if composing == nil then composing = true end
    if has_menu == nil then has_menu = true end
    if is_release == nil then is_release = false end
    local env = make_env(comment, composing, has_menu)
    local key = make_key(repr, is_release)
    local ret = M.func(key, env)
    return ret, (committed or "")
end

local results = {}
local function emit(name, ret, text) results[#results + 1] = { name = name, ret = ret, text = text } end

-- 正常顺位（注释 3 段）
emit("E-第1段", run("Control+e", "hello; hi; hey"))
emit("R-第2段", run("Control+r", "hello; hi; hey"))
emit("T-第3段", run("Control+t", "hello; hi; hey"))
emit("E-大写repr", run("Control+E", "hello; hi; hey"))
emit("T-大写repr", run("Control+T", "hello; hi; hey"))
-- 段数不足：T 放行，E 仍可上屏
emit("T-仅2段吞键", run("Control+t", "hello; hi"))
emit("R-仅2段可上屏", run("Control+r", "hello; hi"))
emit("E-单段无分号放行", run("Control+e", "hello"))
emit("R-单段无分号吞键", run("Control+r", "hello"))
-- 无注释：吞键不输出
emit("E-无注释吞键", run("Control+e", ""))
emit("T-无注释吞键", run("Control+t", ""))
-- 非目标按键 / 释放事件
emit("普通键放行", run("a", "hello; hi"))
emit("释放事件放行", run("Control+e", "hello; hi", true, true, true))
-- 未合成 / 无菜单
emit("未合成放行", run("Control+e", "hello; hi", false, true))
emit("无菜单放行", run("Control+e", "hello; hi", true, false))
-- 尾部省略号 + 首尾空白
emit("T-省略号清理", run("Control+t", "hello; hi; draft…"))
emit("E-首尾空白", run("Control+e", "  hello  ; hi "))
-- 元数据剥离：标准档（词+音标）与完整档（+◆/★/标签/词频）格式
emit("E-剥离音标", run("Control+e", "hello /hә'lәu/; hi /hai/"))
emit("R-剥离音标", run("Control+r", "hello /hә'lәu/; hi /hai/"))
emit("T-完整档剥离", run("Control+t",
    "hello /hә'lәu/ ◆ ★★★ 中·高 BNC2319/2238; hi /hai/ ◆ ★★★ 高 BNC6210/2072; hey /hei/ ◆ ★★ 中 BNC100/200"))
emit("E-多词译词保留", run("Control+e", "very quickly /'veri 'kwikli/; overnight /'әuvәneit/"))
emit("E-极简格式", run("Control+e", "hello; hi"))

return results
"""


def main():
    try:
        import lupa
    except ImportError:
        print("缺少依赖：pip install lupa")
        return 2

    L = lupa.LuaRuntime()
    raw = L.execute(HARNESS, LUA_FILE)
    items = [(i["name"], i["ret"], i["text"]) for i in
             (list(raw.values()) if hasattr(raw, "values") else list(raw))]

    # kAccepted=1, kNoop=2
    expected = {
        "E-第1段":       (1, "hello"),
        "R-第2段":       (1, "hi"),
        "T-第3段":       (1, "hey"),
        "E-大写repr":    (1, "hello"),
        "T-大写repr":    (1, "hey"),
        "T-仅2段吞键":   (1, ""),
        "R-仅2段可上屏": (1, "hi"),
        "E-单段无分号放行": (2, ""),
        "R-单段无分号吞键": (1, ""),
        "E-无注释吞键":  (1, ""),
        "T-无注释吞键":  (1, ""),
        "普通键放行":    (2, ""),
        "释放事件放行":  (2, ""),
        "未合成放行":    (2, ""),
        "无菜单放行":    (2, ""),
        "T-省略号清理":  (1, "draft"),
        "E-首尾空白":    (1, "hello"),
        "E-剥离音标":    (1, "hello"),
        "R-剥离音标":    (1, "hi"),
        "T-完整档剥离":  (1, "hey"),
        "E-多词译词保留": (1, "very quickly"),
        "E-极简格式":    (1, "hello"),
    }

    ok = bad = 0
    for name, ret, text in items:
        want = expected.get(name)
        if want is None:
            continue
        wret, wtext = want
        if ret == wret and text == wtext:
            print("  PASS %-16s ret=%d text=%r" % (name, ret, text))
            ok += 1
        else:
            print("  FAIL %-16s got=(%d,%r) want=(%d,%r)" % (name, ret, text, wret, wtext))
            bad += 1

    print("\n结果：%d 通过，%d 失败" % (ok, bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
