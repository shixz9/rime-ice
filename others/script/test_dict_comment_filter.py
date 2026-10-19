#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dict_comment_filter.lua 的运行时单测（不依赖 Rime/Squirrel，用 lupa 内嵌 LuaJIT 执行）

验证内容：
1. ¤ 元数据解析（音标/星级/牛津◆/考试标签/BNC/FRQ 六段）
2. 三档渲染：极简(0) / 标准(1, 仅音标) / 完整(2, +◆+星级+标签+词频)
3. 旧格式值（不含 ¤）向后兼容
4. 中英双向：分号切分、去词性、max_defs 截断、max_length 兜底
5. 与候选已有 comment 的 append 行为

运行：python3 others/script/test_dict_comment_filter.py
依赖：pip install lupa（LuaJIT 运行时）；读取 opencc/*.txt 真实产物数据
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
LUA_FILE = os.path.join(_HERE, "..", "..", "lua", "dict_comment_filter.lua")
ECDICT_TXT = os.path.join(_HERE, "..", "..", "opencc", "ecdict.txt")
CEDICT_TXT = os.path.join(_HERE, "..", "..", "opencc", "cedict.txt")

HARNESS = r"""
local lua_path, ECDICT, CEDICT = ...
-- lupa 捆绑的 Lua 5.5 移除了全局 yield 别名；librime 侧（Lua 5.4/LuaJIT）不受影响。
-- 单测环境补齐全局别名，使被测模块的 yield(cand) 写法原样可跑。
if rawget(_G, "yield") == nil then rawset(_G, "yield", coroutine.yield) end
local M = dofile(lua_path)

local function make_env(phon, det, ecdict, cedict, maxlen)
    return {
        engine = { context = { get_option = function(self, name)
            if name == "dict_comment" then return true end
            if name == "dict_comment_phonetic" then return phon end
            if name == "dict_comment_detail" then return det end
            return false
        end } },
        ecdict = ecdict or { convert_word = function(self, w) return ECDICT[w] end },
        cedict = cedict or { convert_word = function(self, w) return CEDICT[w] end },
        enable_e2c = true, enable_c2e = true,
        max_defs = 3, max_length = maxlen or 45,
        comment_prefix = " ", comment_suffix = "",
    }
end

local function run(cand, phon, det, env_override)
    local env = env_override or make_env(phon, det)
    local done = false
    local input = { iter = function(self)
        return function()
            if done then return nil end
            done = true
            return cand
        end
    end }
    local co = coroutine.wrap(function() M.func(input, env) end)
    local out = nil
    for c in co do out = c.comment or "" end
    return out or ""
end

local results = {}
local function emit(name, comment) results[#results + 1] = { name = name, comment = comment } end

-- 中译英（c2e，cedict）
emit("c2e-中国-完整", run({ text = "中国" }, true, true))
emit("c2e-你好-完整", run({ text = "你好" }, true, true))         -- 默认 max_length=45，验证截断
emit("c2e-你好-长注释", run({ text = "你好" }, true, true,
    make_env(true, true, nil, nil, 100)))                        -- 放宽长度，验证双译词完整渲染
emit("c2e-电脑-完整", run({ text = "电脑" }, true, true))
emit("c2e-电脑-标准", run({ text = "电脑" }, true, false))         -- 标准档只到音标（牛津◆不上标准档）
emit("c2e-中国-标准", run({ text = "中国" }, true, false))
emit("c2e-中国-极简", run({ text = "中国" }, false, false))
emit("c2e-你-标准", run({ text = "你" }, true, false))            -- 尾字节 0xA0 回归哨兵
emit("c2e-你-完整", run({ text = "你" }, true, true))
-- 英译中（e2c，ecdict）
emit("e2c-chain-完整", run({ text = "chain" }, true, true))
emit("e2c-chain-标准", run({ text = "chain" }, true, false))
emit("e2c-chain-极简", run({ text = "chain" }, false, false))
emit("e2c-China-完整", run({ text = "China" }, true, true))
-- 旧格式 / 无元数据 / 未收录
emit("e2c-asdf-无meta", run({ text = "asdf" }, true, true))
emit("c2e-未收录词", run({ text = "不存在词xyz" }, true, true))
-- 已有 comment 的 append
emit("e2c-chain-带已有注释", run({ text = "chain", comment = "拆字" }, true, true))

-- 合成：max_defs 截断（元数据在整值末尾，真实格式）
do
    local E = {}
    for k, v in pairs(ECDICT) do E[k] = v end
    E["manydefs"] = "一; 二; 三; 四¤p1¤★¤◆¤中¤100¤200"
    local env = make_env(true, true, { convert_word = function(self, w) return E[w] end })
    emit("e2c-maxdefs-截断", run({ text = "manydefs" }, true, true, env))
end

-- 合成：max_length 兜底（头部 60 字 > 45 → 截断加 …，level 0 不渲染元数据）
do
    local E = {}
    for k, v in pairs(ECDICT) do E[k] = v end
    E["longword"] = string.rep("很", 60) .. "¤" .. string.rep("p", 40)
    local env = make_env(false, false, { convert_word = function(self, w) return E[w] end })
    emit("e2c-maxlength-兜底", run({ text = "longword" }, false, false, env))
end

-- 合成：中译英多候选、各自带元数据（放宽 max_length 验证完整渲染）
do
    local C = {}
    for k, v in pairs(CEDICT) do C[k] = v end
    C["测试词"] = "n. alpha¤ælfə¤★★★¤◆¤中¤100¤200; vt. beta¤bi:tə¤★★¤◆¤高¤300¤400"
    local env = make_env(true, true, nil, { convert_word = function(self, w) return C[w] end }, 100)
    emit("c2e-多候选-完整", run({ text = "测试词" }, true, true, env))
end

-- 合成：第三项超长，长度截断应标记 ⋯
do
    local C = {}
    for k, v in pairs(CEDICT) do C[k] = v end
    C["长三"] = "n. alpha¤m1; vt. beta¤m2; adj. " .. string.rep("x", 80) .. "¤m3"
    local env = make_env(false, false, nil, { convert_word = function(self, w) return C[w] end }, 60)
    emit("c2e-第三项超长", run({ text = "长三" }, false, false, env))
end

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


def main():
    try:
        import lupa
    except ImportError:
        print("缺少依赖：pip install lupa")
        return 2

    ec = load_dict(ECDICT_TXT, {"chain", "China", "hello", "asdf"})
    ce = load_dict(CEDICT_TXT, {"中国", "你好", "电脑", "你"})
    print("词典样本：ecdict=%s" % sorted(ec))
    print("         cedict=%s" % sorted(ce))

    L = lupa.LuaRuntime()
    ECDICT = L.table_from(ec)
    CEDICT = L.table_from(ce)

    raw = L.execute(HARNESS, LUA_FILE, ECDICT, CEDICT)
    items = list(raw.values()) if hasattr(raw, "values") else list(raw)
    results = [(item["name"], item["comment"]) for item in items]

    expected = {
        "c2e-中国-完整": "China /'tʃainә/ ★★ 中·高 BNC1973/7946",
        "c2e-你好-完整": "hello /hә'lәu/ ◆ ★★★ 中·高 BNC2319/2238; hi /ha⋯",
        "c2e-你好-长注释": "hello /hә'lәu/ ◆ ★★★ 中·高 BNC2319/2238; hi /hai/ ◆ ★★★ 高 BNC6210/2072",
        "c2e-电脑-完整": "computer /kәm'pju:tә/ ◆ ★★★★ 中·高·雅 BNC580/589",
        "c2e-电脑-标准": "computer /kәm'pju:tә/",
        "c2e-中国-标准": "China /'tʃainә/",
        "c2e-中国-极简": "China",
        "c2e-你-标准": "you /ju:/",
        "c2e-你-完整": "you /ju:/ ◆ ★★★★★ 中·高 BNC15/14",
        "e2c-chain-完整": "链 /tʃein/ ◆ ★★★ 高·四·六·研 BNC1835/1805",
        "e2c-chain-标准": "链 /tʃein/",
        "e2c-chain-极简": "链",
        "e2c-China-完整": "中国 /'tʃainә/ ★★ 中·高 BNC1973/7946",
        "e2c-asdf-无meta": "asdf",
        "c2e-未收录词": "",
        "e2c-chain-带已有注释": "拆字 链 /tʃein/ ◆ ★★★ 高·四·六·研 BNC1835/1805",
        "c2e-多候选-完整": "alpha /ælfə/ ◆ ★★★ 中 BNC100/200; beta /bi:tə/ ◆ ★★ 高 BNC300/400",
    }

    ok = bad = 0
    for name, got in results:
        if name in expected:
            want = expected[name]
            if got == want:
                print("  PASS %-22s %r" % (name, got))
                ok += 1
            else:
                print("  FAIL %-22s\n       got =%r\n       want=%r" % (name, got, want))
                bad += 1
        elif name == "e2c-maxdefs-截断":
            if got.startswith("一；二；三") and "…" in got and "四" not in got \
                    and "/p1/" in got:
                print("  PASS %-22s %r" % (name, got))
                ok += 1
            else:
                print("  FAIL %-22s got=%r" % (name, got))
                bad += 1
        elif name == "c2e-第三项超长":
            if got.startswith("alpha; beta; xxx") and got.endswith("⋯") and "m3" not in got:
                print("  PASS %-22s %r" % (name, got))
                ok += 1
            else:
                print("  FAIL %-22s got=%r" % (name, got))
                bad += 1
        elif name == "e2c-maxlength-兜底":
            if len(got) == 46 and got.endswith("…") and "p" not in got:
                print("  PASS %-22s len=%d" % (name, len(got)))
                ok += 1
            else:
                print("  FAIL %-22s len=%d got=%r" % (name, len(got), got[:60]))
                bad += 1

    print("\n结果：%d 通过，%d 失败" % (ok, bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
