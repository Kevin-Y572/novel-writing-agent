# -*- coding: utf-8 -*-
r"""
修复正文中的"第N章"引用（机器痕迹）— 一次性修复工具
=================================================
用法：python repair_chapter_refs.py <项目名>
- 定位含 第\d+章 的章节
- LLM 定向改写：仅把章节号引用改为故事内时间/事件指代，其余逐字保留
- 校验：输出无残留且长度 ±8% 内；失败则退化为"先前"替换；再失败保留原稿
- 回写 chapter_contents.json 并重新导出成品
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api_client import get_client
from project_manager import ProjectManager
from export import export_manuscript, export_fanqie_package, clean_chapter_text

REF_RE = re.compile(r"第\s*\d+\s*章")


def repair_body(body: str) -> tuple:
    """返回 (修复后正文, 方式)"""
    matches = sorted(set(REF_RE.findall(body)))
    system = ("你是小说编辑。下面的正文中混入了章节号引用（如「第2章」「第15章」），"
              "这是把资料编号写进了故事，必须修复。")
    user = f"""请修复以下正文中的章节号引用。

【修复规则】：
- 只修改含「第N章」的句子：把章节号指代改为故事内的时间/事件指代（如"上次""当初""三天前""遗迹深层那次"）
- 其余内容逐字保留，禁止润色、增删情节、改变人称
- 涉及到的引用：{", ".join(matches)}

【正文】
{body}

请直接输出修复后的完整正文，不要解释。"""
    try:
        fixed = get_client().chat(system, user, temperature=0.3, max_tokens=8192)
    except Exception as e:
        print(f"    LLM 调用失败: {e}")
        fixed = ""
    fixed = (fixed or "").strip()
    if fixed and not REF_RE.search(fixed) and 0.92 <= len(fixed) / max(len(body), 1) <= 1.08:
        return fixed, "llm"
    # 退化：确定性替换（观测到的语境均为"第N章+事件"结构，"先前"可通读）
    fallback = REF_RE.sub("先前", body)
    if not REF_RE.search(fallback):
        return fallback, "deterministic"
    return body, "failed"


def main(project_name: str):
    pm = ProjectManager(project_name)
    state = pm.load(project_name)
    if state is None:
        print(f"✗ 项目 {project_name} 不存在")
        sys.exit(1)

    repaired, skipped = [], []
    for key in sorted(state.chapter_contents, key=int):
        data = state.chapter_contents[key]
        raw = data.get("content", "")
        if not REF_RE.search(raw):
            continue
        body, tail = raw, ""
        marker = "---写作检查---"
        if marker in raw:
            body, tail = raw.split(marker, 1)
            tail = marker + tail
        fixed, method = repair_body(body.strip())
        if method == "failed":
            skipped.append(int(key))
            print(f"  ✗ 第 {key} 章修复失败，保留原稿")
            continue
        data["content"] = fixed + ("\n\n" + tail if tail else "")
        data["word_count"] = len(clean_chapter_text(fixed))
        repaired.append((int(key), method))
        print(f"  ✓ 第 {key} 章（{method}）")

    if repaired:
        pm.save_chapter_content(repaired[0][0],
                                state.chapter_contents[str(repaired[0][0])])
        # save_chapter_content 只写单章 dict 的整个文件，逐章调用保险起见全写一遍
        for num, _ in repaired[1:]:
            pm.save_chapter_content(num, state.chapter_contents[str(num)])
        print(f"共修复 {len(repaired)} 章；失败 {len(skipped)} 章 {skipped}")

        # 重新导出
        book = {}
        book_path = os.path.join(pm.project_dir, "book.json")
        if os.path.exists(book_path):
            book = json.load(open(book_path, encoding="utf-8"))
        chapters = []
        for k in sorted(state.chapter_contents, key=int):
            c = state.chapter_contents[k]
            o = state.chapter_outlines.get(k, {})
            chapters.append((int(k), o.get("chapter_title") or c.get("title", ""),
                             clean_chapter_text(c.get("content", ""))))
        paths = export_manuscript(chapters, pm.project_dir,
                                  book.get("title", project_name), book.get("blurb", ""))
        export_fanqie_package(chapters, pm.project_dir)
        print(f"重新导出：{paths['txt']}")
    else:
        print("无需修复")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "longrun_50ch")
