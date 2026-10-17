#!/usr/bin/env ruby
# frozen_string_literal: true
# check_custom_patch.rb — Rime 自定义补丁漂移检查
#
# 背景：Rime 的 *.custom.yaml patch 对【列表】是整体替换而非合并，
#   因此 default.custom.yaml 的 key_binder.bindings、rime_ice.custom.yaml 的
#   engine/processors 必须完整包含上游条目，否则上游更新会静默丢失。
#   本脚本在每次 git pull 更新后运行，检测三类问题：
#     DRIFT  —— 上游 default.yaml / rime_ice.schema.yaml 中的条目未出现在补丁
#     MISSING—— schema_list 中的方案或 import_tables 中的词库文件不存在
#     SYNTAX —— YAML 解析失败
#
# 用法（在 Rime 配置目录下执行）：
#   ruby others/script/check_custom_patch.rb
#
# 退出码：0 = 通过；1 = 存在问题

require "yaml"

RIME_DIR = File.expand_path("../..", __dir__)

def load_yaml(path)
  YAML.load_file(path)
rescue Psych::Exception => e
  abort "SYNTAX YAML 解析失败: #{path}\n       #{e.message}"
end

# 统一转为 String 键 / String 值，避免 Symbol 与 String 比较不一致
def deep_stringify(obj)
  case obj
  when Hash  then obj.each_with_object({}) { |(k, v), h| h[k.to_s] = deep_stringify(v) }
  when Array then obj.map { |v| deep_stringify(v) }
  else obj
  end
end

# 绑定条目规范化为可比较的字符串
def canon(hash)
  hash.keys.sort.map { |k| "#{k}=#{hash[k]}" }.join(";")
end

# 从 patch 中取路径值，兼容两种 Rime 补丁写法：
#   嵌套形式  patch: { engine: { processors: [...] } }
#   扁平形式  patch: { "engine/processors": [...] }
def patch_at(custom, path)
  p = custom["patch"]
  return nil unless p.is_a?(Hash)

  return p[path] if p.key?(path) # 扁平斜杠形式

  *heads, last = path.split("/")
  heads.each { |h| p = p.is_a?(Hash) ? p[h] : nil }
  p.is_a?(Hash) ? p[last] : nil
end

problems = []

default   = deep_stringify(load_yaml(File.join(RIME_DIR, "default.yaml")))
default_c = deep_stringify(load_yaml(File.join(RIME_DIR, "default.custom.yaml")))
schema    = deep_stringify(load_yaml(File.join(RIME_DIR, "rime_ice.schema.yaml")))
schema_c  = deep_stringify(load_yaml(File.join(RIME_DIR, "rime_ice.custom.yaml")))

ids = lambda do |list|
  (list || []).map { |e| e.is_a?(Hash) ? e["schema"] : e.to_s }
end

# 1. schema_list：生效列表 + 方案文件存在性
upstream_ids = ids.call(default["schema_list"])
effective    = patch_at(default_c, "schema_list") || default["schema_list"]
effective_ids = ids.call(effective)
puts "== schema_list: 上游 #{upstream_ids.size} 个 / 生效 #{effective_ids.size} 个"
removed = upstream_ids - effective_ids
puts "   主动移除: #{removed.join(", ")}" unless removed.empty?
added   = effective_ids - upstream_ids
puts "   本地新增: #{added.join(", ")}" unless added.empty?
effective_ids.each do |id|
  f = File.join(RIME_DIR, "#{id}.schema.yaml")
  problems << "MISSING 方案文件不存在: #{id}.schema.yaml" unless File.exist?(f)
end

# 2. key_binder.bindings 漂移（default.yaml → default.custom.yaml）
up_bindings = default.dig("key_binder", "bindings") || []
cu_bindings = patch_at(default_c, "key_binder/bindings")
if cu_bindings.nil?
  puts "== key_binder.bindings: default.custom.yaml 未覆盖，直接沿用上游（无漂移风险）"
else
  cu_canon = cu_bindings.map { |b| canon(b) }
  missing  = up_bindings.reject { |b| cu_canon.include?(canon(b)) }
  puts "== key_binder.bindings: 上游 #{up_bindings.size} 条 / 补丁 #{cu_bindings.size} 条"
  if missing.empty?
    puts "   OK 上游条目全部在补丁中"
  else
    missing.each { |b| puts "   DRIFT 未同步到 default.custom.yaml: {#{canon(b).tr(";", ", ")}}" }
    problems << "key_binder.bindings 有 #{missing.size} 条上游条目未同步"
  end
end

# 3. engine/processors 漂移（rime_ice.schema.yaml → rime_ice.custom.yaml）
up_procs = schema.dig("engine", "processors") || []
cu_procs = patch_at(schema_c, "engine/processors")
if cu_procs.nil?
  puts "== engine/processors: rime_ice.custom.yaml 未覆盖，直接沿用上游（无漂移风险）"
else
  missing = up_procs - cu_procs
  puts "== engine/processors: 上游 #{up_procs.size} 个 / 补丁 #{cu_procs.size} 个"
  if missing.empty?
    puts "   OK 上游条目全部在补丁中"
  else
    missing.each { |p| puts "   DRIFT 未同步到 rime_ice.custom.yaml: #{p}" }
    problems << "engine/processors 有 #{missing.size} 个上游条目未同步"
  end
end

# 4. import_tables 词库文件存在性（rime_ice + melt_eng）
%w[rime_ice.dict.yaml melt_eng.dict.yaml].each do |dict_file|
  dict = deep_stringify(load_yaml(File.join(RIME_DIR, dict_file)))
  imports = dict["import_tables"] || []
  imports.each do |name|
    f = File.join(RIME_DIR, "#{name}.dict.yaml")
    next if File.exist?(f)

    problems << "MISSING 词库文件不存在: #{name}.dict.yaml（#{dict_file} import_tables 引用）"
  end
  puts "== #{dict_file}: import_tables #{imports.size} 个均存在" if imports.all? { |n| File.exist?(File.join(RIME_DIR, "#{n}.dict.yaml")) }
end

# 5. rime_ice.custom.yaml 追加索引（学习元数据档位开关 + 快捷键）
#    switches/@N、key_binder/bindings/@N 的索引基于上游列表长度；
#    上游增删条目后应重基线（用设置面板保存一次即自动按上游长度重算）。
patch_h   = schema_c["patch"].is_a?(Hash) ? schema_c["patch"] : {}
sw_keys   = patch_h.keys.grep(/\Aswitches\/@\d+\z/)
bd_keys   = patch_h.keys.grep(/\Akey_binder\/bindings\/@\d+\z/)
custom_sw = sw_keys.filter_map { |k| patch_h[k].is_a?(Hash) ? patch_h[k]["name"] : nil }
custom_bd = bd_keys.filter_map { |k| patch_h[k].is_a?(Hash) ? patch_h[k]["accept"] : nil }
up_sw_n   = (schema["switches"] || []).size
up_bd_n   = (schema.dig("key_binder", "bindings") || []).size
puts "== 追加索引: 上游 switches #{up_sw_n} 个 / bindings #{up_bd_n} 条"
%w[dict_comment_phonetic dict_comment_detail].each do |name|
  problems << "MISSING rime_ice.custom.yaml 缺少学习档位开关: #{name}" unless custom_sw.include?(name)
end
problems << "MISSING rime_ice.custom.yaml 缺少快捷键: Control+Shift+Y" unless custom_bd.include?("Control+Shift+Y")
problems << "MISSING rime_ice.custom.yaml 缺少快捷键: Control+Shift+X" unless custom_bd.include?("Control+Shift+X")
sw_idx = sw_keys.map { |k| k[/\d+/].to_i }
bd_idx = bd_keys.map { |k| k[/\d+/].to_i }
if sw_idx.all? { |i| i >= up_sw_n } && bd_idx.all? { |i| i >= up_bd_n }
  puts "   OK 追加索引位于列表末尾（switches@#{sw_idx.join("/")}，bindings@#{bd_idx.join("/")}）"
else
  puts "   WARN 追加索引落后于上游长度（switches@#{sw_idx.join("/")} vs 上游 #{up_sw_n}，" \
       "bindings@#{bd_idx.join("/")} vs 上游 #{up_bd_n}）"
  puts "        不影响功能，但自定义项会插在上游新条目之前；用设置面板保存一次即自动重基线"
end

# 汇总
puts
if problems.empty?
  puts "PASS 未发现漂移或缺失"
else
  puts "FAIL 发现 #{problems.size} 个问题："
  problems.each { |p| puts "  - #{p}" }
  exit 1
end
