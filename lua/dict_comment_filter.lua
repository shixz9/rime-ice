--[[
双向词典释义滤镜 (dict_comment_filter.lua)

功能：
1. 输入中文字词时，在候选词 comment 中显示对应英文释义（基于 CC-CEDICT 词典）。
2. 输入英文单词时，在候选词 comment 中显示对应中文释义（基于 ECDICT 词典）。
3. 释义数量限制（按释义项个数截断，如保留前 2 条释义，超出追加省略号“…”）。
4. 安全 UTF-8 分割：杜绝全角标点字节碰撞导致的汉字乱码（黑框问号）。
5. 全局单例缓存（Singleton Cache）：多软件切换零延迟。
6. 与现有候选 comment（错音错字提示、Emoji、拆字编码等）无缝共存。
7. 英语学习元数据（2026-10-17，P1 数据富化）：词典值附加 ¤ 分隔的
   音标 / 柯林斯星级（★1-5）/ 牛津核心词（◆）/ 考试标签（中高六十研托雅G）/
   BNC / FRQ 词频（由 others/script/build_dict_opencc.py 生成），
   按两个布尔开关分档渲染（librime 的 context:get_option 仅返回布尔值，
   多状态开关的索引不可读，故用两个开关组合三档）：
   - 均关                          → 极简（仅释义，即增强前的行为）
   - dict_comment_phonetic 开      → 标准（释义 + 音标 + ◆）
   - dict_comment_detail 开        → 完整（再叠加 ★ 星级 + 考试标签 + 词频）
   旧格式词典值（不含 ¤）不进入元数据分支，行为与增强前完全一致。
--]]

local M = {}

-- 全局单例缓存：避免每次切换应用/新建 Session 时重复解析大词典导致卡顿
local global_ecdict = nil
local global_cedict = nil
local is_dict_loaded = false

local function load_opencc_dict(name)
    local ok, obj = pcall(Opencc, name)
    if ok and obj then return obj end
    local ok2, obj2 = pcall(Opencc, "opencc/" .. name)
    if ok2 and obj2 then return obj2 end
    return nil
end

local function init_global_dicts()
    if is_dict_loaded then return end
    is_dict_loaded = true
    global_ecdict = load_opencc_dict("ecdict.json")
    global_cedict = load_opencc_dict("cedict.json")
end

local function is_english(s)
    if not s or s == "" then return false end
    return s:match("^[%a%-%'%s%.%d]+$") ~= nil
end

local function is_chinese(s)
    if not s or s == "" then return false end
    for i = 1, #s do
        if s:byte(i) > 127 then
            return true
        end
    end
    return false
end

-- 学习元数据字段分隔符 ¤（U+00A4）的 UTF-8 字节序列。
-- Lua 字符串按字节匹配，必须用两字节写法（单字节 "\164" 匹配不到）。
local function split_meta(value)
    local head, rest = value:match("^(.-)\194\164(.*)$")
    if not head then return value, nil end
    local meta = {}
    for seg in (rest .. "\194\164"):gmatch("(.-)\194\164") do
        meta[#meta + 1] = seg
    end
    return head, meta
end

-- 元数据渲染：level 0=极简（不渲染）/ 1=标准（音标+◆）/ 2=完整（+星级+标签+词频）
local function render_meta(meta, level)
    if not meta or not level or level < 1 then return "" end
    local out = {}
    if level >= 1 then
        if meta[1] and meta[1] ~= "" then out[#out + 1] = "/" .. meta[1] .. "/" end
        if meta[3] and meta[3] ~= "" then out[#out + 1] = meta[3] end
    end
    if level >= 2 then
        if meta[2] and meta[2] ~= "" then out[#out + 1] = meta[2] end
        if meta[4] and meta[4] ~= "" then out[#out + 1] = meta[4] end
        local freq = ""
        if meta[5] and meta[5] ~= "" then freq = "BNC" .. meta[5] end
        if meta[6] and meta[6] ~= "" then
            if freq ~= "" then
                freq = freq .. "/" .. meta[6]
            else
                freq = "FRQ" .. meta[6]
            end
        end
        if freq ~= "" then out[#out + 1] = freq end
    end
    if #out == 0 then return "" end
    return " " .. table.concat(out, " ")
end

-- 按照释义项个数截断，安全处理 UTF-8 字符
-- 本地修改：去除英文词性标注（int. n. v. adj. 等），只保留释义本身
local POS_TAGS = {
    "int", "n", "v", "vt", "vi", "adj", "adv", "prep", "conj", "pron",
    "aux", "num", "art", "abbr", "phr", "sl", "fig", "lit", "derog",
    "colloq", "formal", "informal", "TCM", "Budd", "obs", "hum", "rare"
}
local function strip_pos(s)
    local changed = true
    while changed do
        changed = false
        for _, tag in ipairs(POS_TAGS) do
            local stripped = s:gsub("^" .. tag .. "%.%s*", "")
            if stripped ~= s then
                s = stripped
                changed = true
            end
        end
    end
    return s
end

-- 字符长度安全兜底（UTF-8 字符计数与截断），与原有逻辑一致
local function apply_length_guard(result, max_length)
    local has_more = false
    if max_length and max_length > 0 then
        local ulen = utf8.len(result)
        if ulen and ulen > max_length then
            local byte_offset = utf8.offset(result, max_length + 1)
            if byte_offset then
                result = result:sub(1, byte_offset - 1)
                has_more = true
            end
        elseif not ulen and #result > max_length then
            result = result:sub(1, max_length)
            has_more = true
        end
    end
    return result, has_more
end

-- 全角/中文标点归一为 ASCII 分号（元数据段内已由构建脚本保证无这些字符）
local function normalize_seps(s)
    local t = s:gsub("\194\160", " ")
    t = t:gsub("；", ";")
    t = t:gsub("，", ";")
    t = t:gsub("、", ";")
    t = t:gsub(",", ";")
    return t
end

-- 英译中：元数据已整体拆出，头部按既有逻辑格式化（全角分号连接）
local function format_head(def_str, max_defs, max_length)
    if not def_str or def_str == "" then return "" end

    local items = {}
    for item in normalize_seps(def_str):gmatch("([^;]+)") do
        local trimmed = item:match("^%s*(.-)%s*$") or ""
        trimmed = strip_pos(trimmed)
        if trimmed ~= "" then
            table.insert(items, trimmed)
        end
    end

    local result = ""
    local has_more = false

    if #items == 0 then
        result = def_str
    elseif #items <= max_defs then
        result = table.concat(items, "；")
    else
        local selected = {}
        for i = 1, max_defs do
            table.insert(selected, items[i])
        end
        result = table.concat(selected, "；")
        has_more = true
    end

    local truncated
    result, truncated = apply_length_guard(result, max_length)
    if truncated then has_more = true end

    if has_more then
        result = result .. "…"
    end

    return result
end

-- 中译英：分号切分，逐项剥离元数据与词性后渲染
local function format_c2e(def_str, max_defs, max_length, level)
    if not def_str or def_str == "" then return "" end

    local items = {}
    for item in normalize_seps(def_str):gmatch("([^;]+)") do
        local trimmed = item:match("^%s*(.-)%s*$") or ""
        local head, meta = split_meta(trimmed)
        head = strip_pos(head)
        if head and head ~= "" then
            items[#items + 1] = head .. render_meta(meta, level)
        end
    end

    if #items == 0 then return def_str end

    local result
    local has_more = false
    if #items <= max_defs then
        result = table.concat(items, "; ")
    else
        local selected = {}
        for i = 1, max_defs do
            table.insert(selected, items[i])
        end
        result = table.concat(selected, "; ")
        has_more = true
    end

    local truncated
    result, truncated = apply_length_guard(result, max_length)
    if truncated then has_more = true end

    if has_more then
        result = result .. "…"
    end

    return result
end

-- 英译中入口：拆整体元数据 → 头部格式化 → 尾部按档位渲染
local function format_e2c(def_str, max_defs, max_length, level)
    if not def_str or def_str == "" then return "" end
    local head, meta = split_meta(def_str)
    local formatted = format_head(head, max_defs, max_length)
    return formatted .. render_meta(meta, level)
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

function M.init(env)
    local config = env.engine.schema.config
    local ns = env.name_space:gsub("^*", "")
    if ns == "" then ns = "dict_comment_filter" end

    -- 开关配置
    env.enable_c2e = config:get_bool(ns .. "/enable_chinese_to_english")
    if env.enable_c2e == nil then env.enable_c2e = true end

    env.enable_e2c = config:get_bool(ns .. "/enable_english_to_chinese")
    if env.enable_e2c == nil then env.enable_e2c = true end

    -- 释义显示项数（默认保留前 2 条释义）
    env.max_defs = config:get_int(ns .. "/max_defs") or 2
    -- 最大字符长度安全兜底（默认 45 字符）
    env.max_length = config:get_int(ns .. "/max_length") or 45

    -- 释义前缀/后缀
    env.comment_prefix = config:get_string(ns .. "/comment_prefix") or " "
    env.comment_suffix = config:get_string(ns .. "/comment_suffix") or ""

    -- 确保全局单例字典已加载（仅在进程初次调用时加载一次）
    init_global_dicts()
    env.ecdict = global_ecdict
    env.cedict = global_cedict
end

function M.func(input, env)
    local context = env.engine.context
    -- 支持通过快捷键或方案菜单全局开关词典释义 (dict_comment)
    local is_dict_on = context:get_option("dict_comment")
    if is_dict_on == false then
        for cand in input:iter() do
            yield(cand)
        end
        return
    end

    -- 学习档位：两个布尔开关组合三档（详见文件头注释）
    local phonetic = context:get_option("dict_comment_phonetic") == true
    local detail = context:get_option("dict_comment_detail") == true
    local level = detail and 2 or (phonetic and 1 or 0)

    for cand in input:iter() do
        local text = cand.text
        local raw_def = nil
        local is_e2c = false

        if env.enable_e2c and env.ecdict and is_english(text) then
            -- 英译中：优先转为小写查询，若未命中则查原词
            local lower_text = text:lower():gsub("^%s+", ""):gsub("%s+$", "")
            raw_def = lookup(env.ecdict, lower_text) or lookup(env.ecdict, text)
            is_e2c = true
        elseif env.enable_c2e and env.cedict and is_chinese(text) then
            -- 中译英
            local clean_text = text:gsub("^%s+", ""):gsub("%s+$", "")
            raw_def = lookup(env.cedict, clean_text)
            is_e2c = false
        end

        if raw_def and raw_def ~= "" then
            local formatted_def
            if is_e2c then
                formatted_def = format_e2c(raw_def, env.max_defs, env.max_length, level)
            else
                formatted_def = format_c2e(raw_def, env.max_defs, env.max_length, level)
            end
            if formatted_def ~= "" then
                local formatted = env.comment_prefix .. formatted_def .. env.comment_suffix
                if cand.comment and cand.comment ~= "" then
                    cand.comment = cand.comment .. formatted
                else
                    local prefix = env.comment_prefix:match("^%s*(.-)$") or ""
                    cand.comment = prefix .. formatted_def .. env.comment_suffix
                end
            end
        end

        yield(cand)
    end
end

return M
