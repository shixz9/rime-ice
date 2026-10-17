-- comment_rank_commit.lua
-- 中文候选的英文注释（如「你好」→ "hello; hi"）按分号拆分为多个英文词：
--   Ctrl+E → 上屏注释中的第 1 个英文词（hello）
--   Ctrl+R → 上屏注释中的第 2 个英文词（hi）
-- 仅处理含 ASCII 分号的注释（中译英方向的拼接格式）；
-- 英文候选的中文注释用全角「；」，不会进入此分支，按键放行给编辑器原逻辑。
-- 候选无注释时吞键（不输出），避免回落 commit_comment 上屏空字符串。

local kAccepted = 1
local kNoop = 2

local M = {}

function M.func(key, env)
    if key:release() then return kNoop end
    local repr = key:repr()
    local idx
    if repr == "Control+e" or repr == "Control+E" then
        idx = 1
    elseif repr == "Control+r" or repr == "Control+R" then
        idx = 2
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
    for item in comment:gmatch("([^;]+)") do
        item = item:gsub("^%s+", ""):gsub("%s+$", "")
        item = item:gsub("…%s*$", ""):gsub("%s+$", "")
        if item ~= "" then
            table.insert(items, item)
        end
    end
    if #items < idx then return kNoop end

    env.engine:commit_text(items[idx])
    ctx:clear()
    return kAccepted
end

return M
