--[[
词典释义快捷上屏处理器 (dict_commit_processor.lua)

功能：
在输入拼音或英文时，挑选并选中候选词后：
按下快捷键（推荐：Ctrl+D 或反斜杠 "\"）：
将「当前高亮候选词 + 完整词典释义」一同上屏。

快捷键设计说明：
- 避免使用 Alt 系列组合键（Windows 系统底层会将 Alt+Enter 拦截用于“文件属性”或系统菜单）
- 默认支持：
  1. Ctrl + D  (Dict 词典，推荐，单手操作最舒适)
  2. \         (反斜杠键，无需按 Ctrl，选词时一键输出完整释义)
  3. Ctrl + Shift + Return (回车组合键)
- 支持在 schema 或 custom.yaml 中通过 `key_binder/dict_commit` 自定义任意按键。
- Ctrl+E / Ctrl+R / Ctrl+T 由 comment_rank_commit.lua 负责，本处理器不再拦截 Ctrl+E。

示例：
- 选中「你好」按 Ctrl+D -> 上屏「你好 (hello; hi; how are you)」
- 选中「apple」按 Ctrl+D -> 上屏「apple (苹果)」

注意：OpenCC 原始值里的学习元数据（音标/星级/◆/考试标签/BNC/FRQ，
以 ¤ 分隔）只用于候选注释，上屏前必须清洗掉；词性标注（n. / int. …）
也一并去除，保持「单词 + 纯释义」的可读格式。
--]]

local M = {}

local global_ecdict = nil
local global_cedict = nil

local function load_opencc_dict(name)
    local ok, obj = pcall(Opencc, name)
    if ok and obj then return obj end
    local ok2, obj2 = pcall(Opencc, "opencc/" .. name)
    if ok2 and obj2 then return obj2 end
    return nil
end

local function init_dicts()
    if not global_ecdict then global_ecdict = load_opencc_dict("ecdict.json") end
    if not global_cedict then global_cedict = load_opencc_dict("cedict.json") end
end

local function is_english(s)
    if not s or s == "" then return false end
    -- 显式 ASCII 空白类：避免 Lua 5.5 / lupa 把 UTF-8 尾字节 0xA0 当空白
    return s:match("^[%a%-%' \t\n\v\f\r%.%d]+$") ~= nil
end

local function lookup(opencc_obj, key)
    if not opencc_obj or not key or key == "" then return nil end
    -- 仅使用 convert_word 进行精确全词匹配（严禁使用 convert_text，以防分词匹配局部子词导致释义错误）
    local ok, res = pcall(function() return opencc_obj:convert_word(key) end)
    if ok and res then
        if type(res) == "table" and #res > 0 then
            local joined = table.concat(res, " ")
            if joined ~= key and joined ~= "" then
                return joined
            end
        elseif type(res) == "string" and res ~= key and res ~= "" then
            return res
        end
    end
    return nil
end

-- ── 释义清洗 ────────────────────────────────────────────────────────────
-- 词性表与 dict_comment_filter.lua 保持一致；改动时请同步两处。
local POS_TAGS = {
    "int", "n", "v", "vt", "vi", "adj", "adv", "prep", "conj", "pron",
    "aux", "num", "art", "abbr", "phr", "sl", "fig", "lit", "derog",
    "colloq", "formal", "informal", "TCM", "Budd", "obs", "hum", "rare"
}

local function trim_ws(s)
    if not s then return s end
    s = s:gsub("^[ \t\n\v\f\r]+", "")
    s = s:gsub("[ \t\n\v\f\r]+$", "")
    return s
end

local function strip_pos(s)
    local changed = true
    while changed do
        changed = false
        for _, tag in ipairs(POS_TAGS) do
            local stripped = s:gsub("^" .. tag .. "%.[ \t\n\v\f\r]*", "")
            if stripped ~= s then
                s = stripped
                changed = true
            end
        end
    end
    return s
end

-- 去掉 ¤ 及其后的学习元数据（U+00A4 的 UTF-8 字节为 \194\164）
local function strip_meta(s)
    local idx = s:find("\194\164", 1, true)
    if idx then return s:sub(1, idx - 1) end
    return s
end

-- 把原始词典值清洗成「纯释义1; 纯释义2; …」：
--   1. NBSP 还原为普通空格；
--   2. 英译中（e2c）的元数据整块位于值末尾，先整体截掉再分号切分；
--      中译英（c2e）的元数据逐项挂在每个译词后，切分后逐项截掉；
--   3. 去掉词性、修剪空白；
--   4. 不按 max_defs 截断——Ctrl+D 的语义是“完整释义”。
local function clean_definition(def_str, meta_per_item)
    if not def_str or def_str == "" then return "" end

    local normalized = def_str:gsub("\194\160", " ")
    if not meta_per_item then
        -- e2c：head¤音标¤星级¤牛津¤标签¤BNC¤FRQ，整块元数据在末尾
        normalized = strip_meta(normalized)
    end
    normalized = normalized:gsub("；", ";")
    normalized = normalized:gsub("，", ";")
    normalized = normalized:gsub("、", ";")
    normalized = normalized:gsub(",", ";")

    local items = {}
    for raw in normalized:gmatch("([^;]+)") do
        local item = raw
        if meta_per_item then
            -- c2e：each¤meta; each¤meta
            item = strip_meta(item)
        end
        item = trim_ws(item)
        item = strip_pos(item)
        item = trim_ws(item)
        if item ~= "" then
            items[#items + 1] = item
        end
    end

    return table.concat(items, "; ")
end

function M.init(env)
    init_dicts()
    local config = env.engine.schema.config
    -- 支持在 schema 中配置自定义快捷键，默认为 Control+d
    env.commit_key = config:get_string("key_binder/dict_commit") or "Control+d"
end

function M.func(key, env)
    local engine = env.engine
    local context = env.engine.context

    if not key:release() and (context:is_composing() or context:has_menu()) then
        local repr = key:repr()
        -- 匹配 Ctrl+d、反斜杠 \、Ctrl+Shift+Return 以及自定义按键
        -- 注：本地已移除 Control+e / Control+E，让位给「只上屏释义」(commit_comment)
        local is_match = (repr == env.commit_key)
            or (repr == "Control+d")
            or (repr == "Control+D")
            or (repr == "backslash")
            or (repr == "\\")
            or (repr == "Control+Shift+Return")
            or (repr == "Control+Shift+KP_Enter")

        if is_match then
            init_dicts()
            local cand = context:get_selected_candidate()
            local text = trim_ws(cand and cand.text or context.input)

            if text and text ~= "" then
                local full_def = nil
                local meta_per_item = true
                if is_english(text) and global_ecdict then
                    local lower = text:lower()
                    full_def = lookup(global_ecdict, lower) or lookup(global_ecdict, text)
                    meta_per_item = false
                elseif global_cedict then
                    full_def = lookup(global_cedict, text)
                end

                local cleaned = clean_definition(full_def, meta_per_item)
                if cleaned ~= "" then
                    engine:commit_text(text .. " (" .. cleaned .. ")")
                else
                    engine:commit_text(text)
                end

                context:clear()
                return 1 -- kAccepted
            end
        end
    end

    return 2 -- kNoop
end

return M
