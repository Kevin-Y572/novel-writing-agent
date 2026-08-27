"""
成稿后处理与导出
=================================================
- 清洗：去检查清单残留、去正文内嵌的重复章节标题、空行规范化
- 统一章节标题为「第X章 标题」（中文数字），修复"第1章/第一章"混排
- 导出：番茄上传用 TXT（单文件）+ Markdown
"""

import os
import re

_CHECKLIST_PATTERNS = [
    re.compile(r"---\s*写作检查\s*---.*\Z", re.S),
    re.compile(r"【写作检查清单】.*\Z", re.S),
]
# 正文中内嵌的章节标题行（导出时会统一重新生成，先剥掉避免重复）
_EMBEDDED_HEADING = re.compile(r"^\s*(#+\s*)?第[0-9一二三四五六七八九十百千零两]{1,7}\s*章[：:：\s].*$")
_MD_HEADING = re.compile(r"^\s*#{1,3}\s+.*$")


def to_chinese_numeral(n: int) -> str:
    """1-99 → 一…九十九（章节号够用）"""
    digits = "零一二三四五六七八九"
    if n < 0:
        return str(n)
    if n < 10:
        return digits[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + digits[n % 10]
    tens, ones = divmod(n, 10)
    return digits[tens] + "十" + (digits[ones] if ones else "")


def clean_chapter_text(raw: str) -> str:
    """单章正文清洗：去清单残留/内嵌标题、规范空行"""
    text = raw or ""
    for pattern in _CHECKLIST_PATTERNS:
        text = pattern.sub("", text)

    lines = text.split("\n")
    cleaned = []
    # 只处理开头 5 行内的内嵌标题（正文中间的正常对话不受影响）
    heading_zone = True
    blank_run = 0
    for line in lines:
        stripped = line.strip()
        if heading_zone and (stripped == "" or _EMBEDDED_HEADING.match(line)
                             or _MD_HEADING.match(line) or stripped.startswith("#")):
            if _EMBEDDED_HEADING.match(line) or _MD_HEADING.match(line):
                continue
            if stripped == "":
                blank_run += 1
                continue
        heading_zone = False
        if stripped == "":
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        cleaned.append(line.rstrip())
    return "\n".join(cleaned).strip()


def export_manuscript(chapters: list, out_dir: str, title: str, blurb: str,
                      book_name: str = "manuscript") -> dict:
    """chapters: [(num, chapter_title, raw_text)]。返回生成的文件路径。
    - {book_name}.txt：番茄上传格式（第X章 标题 + 正文，段间空行）
    - {book_name}.md：带书名/简介的完整稿
    """
    os.makedirs(out_dir, exist_ok=True)

    txt_blocks, md_blocks = [], [f"# {title}", "", f"> {blurb}", ""]
    seen_numbers = set()
    for num, chapter_title, raw in chapters:
        heading = f"第{to_chinese_numeral(num)}章 {clean_chapter_text(chapter_title) or ''}".strip()
        body = clean_chapter_text(raw)
        if num in seen_numbers:
            continue  # 防重：同一章号只导出一次
        seen_numbers.add(num)
        txt_blocks.append(f"{heading}\n\n{body}")
        md_blocks.append(f"\n---\n\n## {heading}\n\n{body}\n")

    txt_path = os.path.join(out_dir, f"{book_name}.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n\n\n".join(txt_blocks))

    md_path = os.path.join(out_dir, f"{book_name}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_blocks))

    return {"txt": txt_path, "md": md_path, "chapters": len(seen_numbers),
            "total_chars": sum(len(clean_chapter_text(t)) for _, _, t in chapters)}


def export_fanqie_package(chapters: list, out_dir: str,
                          sub_dir: str = "fanqie_chapters") -> str:
    """导出逐章 TXT（番茄作者后台批量上传用）：每章一个文件。"""
    import os
    pkg_dir = os.path.join(out_dir, sub_dir)
    os.makedirs(pkg_dir, exist_ok=True)
    for num, chapter_title, raw in chapters:
        heading = f"第{to_chinese_numeral(num)}章 {clean_chapter_text(chapter_title) or ''}".strip()
        body = clean_chapter_text(raw)
        with open(os.path.join(pkg_dir, f"{num:03d}.txt"), "w", encoding="utf-8") as f:
            f.write(f"{heading}\n\n{body}")
    return pkg_dir
