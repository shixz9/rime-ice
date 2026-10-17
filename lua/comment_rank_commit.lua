-- comment_rank_commit.lua
-- 中文候选的英文注释（如「你好」→ "hello; hi"）按分号拆分为多个英文词：
--   Ctrl+E → 上屏注释中的第 1 个英文词（hello）
--   Ctrl+R → 上屏注释中的第 2 个英文词（hi）
--   Ctrl+T → 上屏注释中的第 3 个英文词
-- 仅处理含 ASCII 分号的注释（中译英方向的拼接格式）；
-- 英文候选的中文注释用全角「；」，不会进入此分支，按键放行给编辑器原逻辑。
-- 候选无注释时吞键（不输出），避免回落 commit_comment 上屏空字符串。
-- 注释条数上限见 dict_comment_filter.lua 的 max_defs（默认 3 条）。
--
-- 注释中每个译词可能带学习元数据尾巴（音标 /.../、◆、★、考试标签、
-- BNC/FRQ 词频，见 dict_comment_filter.lua 的 render_meta）。
-- 上屏前必须剥掉元数据，只保留纯英文译词，否则会把音标一起打出来。

local kAccepted = 1
local kNoop = 2

local M = {}

-- 剥掉译词的元数据尾巴：按空格切词，遇到元数据起始段即截断
-- （音标以 / 开头、◆/★、BNC/FRQ 词频；纯英文译词不会以这些开头）
local function strip_meta(item)
    local head = {}
    for tok in item:gmatch("%S+") do
        if tok:match("^/") or tok:match("^◆") or tok:match("^★")
            or tok:match("^BNC") or tok:match("^FRQ") then
            break
        end
        head[#head + 1] = tok
    end
    if #head == 0 then return item end
    return table.concat(head, " ")
end

function M.func(key, env)
    if key:release() then return kNoop end
    local repr = key:repr()
    local idx
    if repr == "Control+e" or repr == "Control+E" then
        idx = 1
    elseif repr == "Control+r" or repr == "Control+R" then
        idx = 2
    elseif repr == "Control+t" or repr == "Control+T" then
        idx = 3
    else
        return kNoop
    end

    local ctx = env.engine.context
    if not (ctx:is_composing() and ctx:has_menu()) then return kNoop end

    -- 当前高亮候选（输入 nihao 时默认就是第一个中文候选）
    local cand = ctx:get_selected_candidate()
    if not cand then return kNoop end
    local comment = cand.comment or ""

    -- 候选无注释：吞键，避免回落 commit_comment 上屏空字符串
    if comment == "" then return kAccepted end

    -- 中译英注释以 ASCII "; " 拼接；无分号（单词数不足）则放行给编辑器
    if not comment:find(";") then return kNoop end

    local items = {}
    -- 注意：Lua 5.5 将 for 循环变量视为 const（循环体内不可再赋值），
    -- 故先取到局部变量再规整；Lua 5.4（librime）同样兼容。
    for raw_item in comment:gmatch("([^;]+)") do
        local item = raw_item
        item = item:gsub("^[ \t\n\v\f\r]+", ""):gsub("[ \t\n\v\f\r]+$", "")
        item = item:gsub("…[ \t\n\v\f\r]*$", ""):gsub("[ \t\n\v\f\r]+$", "")
        if item ~= "" then
            table.insert(items, strip_meta(item))
        end
    end
    if #items < idx then return kNoop end

    env.engine:commit_text(items[idx])
    ctx:clear()
    return kAccepted
end

return M
