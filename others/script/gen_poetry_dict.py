#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 从 chinese-poetry 生成 Rime 诗词词库（挂在 rime_ice.dict.yaml 的 import_tables 下）
#
# 用法:
#   python3 gen_poetry_dict.py [语料目录] [输出目录]
#   语料目录也可用环境变量 POETRY_SRC 指定；缺省为下方 SRC_DEFAULT
#   输出目录缺省 ~/Library/Rime/cn_dicts
#
# 依赖: opencc-python-reimplemented, pypinyin
#   pip install opencc-python-reimplemented pypinyin
#
# 语料结构（chinese-poetry 仓库布局）:
#   全唐诗/poet.tang.*.json + 全唐诗/唐诗补录.json
#   宋词/ci.song.*.json
#   诗经/shijing.json 论语/lunyu.json 楚辞/chuci.json
#   五代诗词/*/*.json
#   蒙学/*.json（文件名含 traditional 的繁体版跳过）
#
# 行为说明:
# - 三个输出共享一个去重集合，按 tang → songci → classics 顺序处理，
#   先出现者保留（权重不同：classics=8，tang/songci=5）
# - classics 做繁→简转换并按逗号再切短句（三字经/弟子规类蒙学需要）
# - 只收 3~18 字片段；pinyin 用 pypinyin 常用读音，诗词破音字可能不准
# - version 写生成当天日期，保证 Rime 重建词典表
import json, glob, os, re, sys, datetime

SRC_DEFAULT = "/Users/shixz9/Documents/kimi/tasks/2026-10-15/00-13-04-0e8032e6/chinese-poetry"
OUT_DEFAULT = os.path.expanduser("~/Library/Rime/cn_dicts")
VERSION = datetime.date.today().isoformat()

SRC = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("POETRY_SRC", SRC_DEFAULT)
OUT = sys.argv[2] if len(sys.argv) > 2 else OUT_DEFAULT

if not os.path.isdir(SRC):
    sys.exit(f"[error] 语料目录不存在: {SRC}\n"
             f"         usage: python3 gen_poetry_dict.py <chinese-poetry目录> [输出目录]")
os.makedirs(OUT, exist_ok=True)

from opencc import OpenCC
cc = OpenCC('t2s')
from pypinyin import lazy_pinyin, Style

SENT_SPLIT = re.compile(r'[。！？!?；;\n\r]+')
# 注：原始版的 r'...""''...' 单引号串会被 '' 提前截断（后半段退化为非 raw 串，
# 报 SyntaxWarning 且直引号未入字符类）；下游 CJK 过滤会删除所有非汉字，二者等价。
PUNCT = re.compile(r"""[，、：:．."'""'「」『』《》〈〉（）()【】\[\]…—·~～\u3000\s・　]+""")
CJK = re.compile(r'[^\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff〇]')

SOURCES = {
    # 输出文件名: (glob列表, 权重, 是否繁体, 额外逗号切分)
    "poetry.tang":    (sorted(glob.glob(f"{SRC}/全唐诗/poet.tang.*.json"))
                       + [f"{SRC}/全唐诗/唐诗补录.json"], 5, False, False),
    "poetry.songci":  (sorted(glob.glob(f"{SRC}/宋词/ci.song.*.json")),  5, False, False),
    "poetry.classics": (
        [f"{SRC}/诗经/shijing.json", f"{SRC}/论语/lunyu.json", f"{SRC}/楚辞/chuci.json"]
        + sorted(glob.glob(f"{SRC}/五代诗词/*/*.json"))
        + [f"{SRC}/蒙学/{f}" for f in sorted(os.listdir(f"{SRC}/蒙学"))
           if f.endswith(".json") and "traditional" not in f],
        8, True, True),  # 额外按逗号切短句（三字经/弟子规等）
}

def iter_texts(node, extra_comma):
    """递归提取文本片段：dict 只下钻 paragraphs/content 字段，str 按句切分"""
    if isinstance(node, str):
        segs = SENT_SPLIT.split(node)
    elif isinstance(node, list):
        for it in node:
            yield from iter_texts(it, extra_comma)
        return
    elif isinstance(node, dict):
        for k in ("paragraphs", "content"):
            if k in node:
                yield from iter_texts(node[k], extra_comma)
                return
        return
    else:
        return
    for s in segs:
        if extra_comma:
            for sub in re.split(r'[，、]+', s):
                yield sub
        else:
            yield s

def clean(line, trad):
    line = PUNCT.sub('', line)
    if trad:
        line = cc.convert(line)
    line = CJK.sub('', line)
    return line

seen = set()
for name, (files, weight, trad, extra) in SOURCES.items():
    out_path = os.path.join(OUT, name + ".dict.yaml")
    entries = []
    n_raw = 0
    for fp in files:
        try:
            data = json.load(open(fp))
        except Exception as e:
            print(f"[skip] {os.path.basename(fp)}: {e}", file=sys.stderr)
            continue
        for raw in iter_texts(data, extra):
            n_raw += 1
            t = clean(raw, trad)
            if not (3 <= len(t) <= 18):
                continue
            if t in seen:
                continue
            seen.add(t)
            entries.append(t)
    with open(out_path, "w") as w:
        w.write("# Rime dictionary\n# encoding: utf-8\n# 由 chinese-poetry 自动生成：整句诗词/经典，权重 %d\n---\nname: %s\nversion: \"%s\"\nsort: by_weight\n...\n" % (weight, name, VERSION))
        for t in entries:
            py = " ".join(lazy_pinyin(t, style=Style.NORMAL, errors='ignore'))
            if py.strip() == "":
                continue
            w.write(f"{t}\t{py}\t{weight}\n")
    print(f"{name}: 原始片段 {n_raw}，入库 {len(entries)} 条 -> {out_path}", flush=True)

print("完成")
