# -*- coding: utf-8 -*-
"""
非交互 CLI — 供 ZCode 插件 / dsh / 脚本调用小说写作流水线
================================================================
子命令：
  new      创建新书项目（书名+简介+设定库+第一卷粗纲）
  chapter  生成指定章节（默认下一章），完整流水线：细纲→审查→写作→校验→维护
  status   查看项目进度
  export   导出全文 manuscript.md
  list     列出书架上全部书籍（三种格式,含当前书标记）
  use      切换「当前书」（未指定 --project-dir 的命令默认作用于当前书）
  memory   查看书籍记忆：设定库 / 章节时间线 / 未回收伏笔

状态持久化在 <project-dir>/project.json，跨调用可续写。
chapter/status/export/memory 未传 --project-dir 时自动使用书架当前书。
"""

import sys
import os
import json
import argparse
from dataclasses import fields as dataclass_fields

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from models import (
    ProjectConfig, SettingLibrary, VolumeOutline,
    CharacterEntry, GeographyEntry, HistoryEntry, PowerSystemEntry, FactionEntry,
)
from bookshelf import (
    BookshelfManager, format_book_table, read_book,
    get_setting_library, get_chapter_timeline, read_hooks,
)
from setting_library import SettingLibraryManager
from rough_outline_agent import RoughOutlineAgent
from detailed_outline_agent import DetailedOutlineAgent
from outline_review_agent import OutlineReviewAgent
from chapter_writing_agent import ChapterWritingAgent
from content_review_agent import ContentReviewAgent
from setting_maintenance_agent import SettingMaintenanceAgent
from write_novel_demo import (
    generate_title_and_blurb, summarize_chapter,
    review_feedback_text, strip_checklist,
)

ENTRY_CLASSES = {
    "characters": CharacterEntry, "geography": GeographyEntry,
    "history": HistoryEntry, "power_system": PowerSystemEntry,
    "factions": FactionEntry,
}

DEFAULT_IDEA = """地球大学生林渊穿越到「魂纹世界」——这个世界的人族在十六岁成年礼上觉醒「魂纹」，
魂纹决定了修炼天赋和战斗方式。林渊觉醒的是一道覆盖全身的「空白魂纹」，被所有人判定为废纹。
但他很快发现，自己的空白魂纹拥有一个逆天能力——拓印：触碰他人的魂纹即可复制其能力。
更可怕的是，他可以把多种魂纹能力组合起来，创造出闻所未闻的战斗方式。"""


# ── 状态序列化 ────────────────────────────────────────────────

def _entry_to_dict(entry) -> dict:
    return {k: v for k, v in entry.__dict__.items()}


def _entry_from_dict(cls, data: dict):
    valid = {f.name for f in dataclass_fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in valid})


def save_state(state: dict, project_dir: str):
    # 深拷贝一份再序列化，不污染调用方内存中的 dataclass 引用
    out = dict(state)
    out["config"] = dict(state["config"].__dict__)
    if state.get("volume_outline") is not None:
        out["volume_outline"] = dict(state["volume_outline"].__dict__)
    lib = state["setting_library"]
    out["setting_library"] = {
        name: {n: _entry_to_dict(e) for n, e in getattr(lib, name).items()}
        for name in ENTRY_CLASSES
    }
    with open(os.path.join(project_dir, "project.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)


def load_state(project_dir: str) -> dict:
    with open(os.path.join(project_dir, "project.json"), encoding="utf-8") as f:
        state = json.load(f)
    lib = SettingLibrary()
    for name, cls in ENTRY_CLASSES.items():
        for n, e in state["setting_library"].get(name, {}).items():
            getattr(lib, name)[n] = _entry_from_dict(cls, e)
    state["setting_library"] = lib
    vo = state.get("volume_outline")
    if vo:
        valid = {f.name for f in dataclass_fields(VolumeOutline)}
        state["volume_outline"] = VolumeOutline(**{k: v for k, v in vo.items() if k in valid})
    state["config"] = _entry_from_dict(ProjectConfig, state["config"])
    return state


# ── 书架辅助 ────────────────────────────────────────────────

def _shelf_dir_name(project_dir: str) -> str:
    """从路径或目录名提取书架目录名"""
    return os.path.basename(os.path.abspath(project_dir).rstrip("\\/"))


def _resolve_project_dir(project_dir: str) -> str:
    """未指定项目目录时,自动使用书架「当前书」"""
    if project_dir:
        return os.path.abspath(project_dir)
    mgr = BookshelfManager()
    mgr.scan()
    cur = mgr.get_current()
    if not cur:
        print("✗ 未指定 --project-dir,且书架上没有「当前书」。可选书籍:")
        print(format_book_table(mgr.books))
        print("\n  切换当前书:python cli.py use --project-dir data/projects/<书名>")
        sys.exit(1)
    print(f"ℹ 当前书:《{cur.title}》(目录 {cur.dir_name})")
    return cur.project_dir


# ── 子命令实现 ────────────────────────────────────────────────

def cmd_new(args):
    project_dir = os.path.abspath(args.project_dir)
    os.makedirs(project_dir, exist_ok=True)
    if os.path.exists(os.path.join(project_dir, "project.json")):
        print(f"✗ 项目已存在：{project_dir}（如需新建请换目录或删除 project.json）")
        sys.exit(1)

    config = ProjectConfig(
        project_name=os.path.basename(project_dir), genre=args.genre,
        narrative_person="第三人称", writing_style=args.style,
        internet_slang_level="中", core_idea=args.idea,
        core_setting=args.core_setting or "", chapters_per_volume=50,
        words_per_chapter=args.words,
    )
    print("① 书名与简介...")
    title, blurb = generate_title_and_blurb(config)
    if args.name:
        title = args.name

    print("② 设定库初始化...")
    lib = SettingLibrary()
    slm = SettingLibraryManager(lib, config)
    settings_ok = True
    try:
        slm.generate_initial_settings()
        print(f"   人物 {len(lib.characters)} / 地理 {len(lib.geography)} / 战力 "
              f"{len(lib.power_system)} / 势力 {len(lib.factions)} / 历史 {len(lib.history)}")
    except Exception as e:
        settings_ok = False
        print(f"   ⚠ 初始化失败（空库继续）: {e}")

    print("③ 第一卷粗纲...")
    rough = RoughOutlineAgent(config, lib).generate_volume_outline(
        volume_number=1, setting_summary=slm.get_summary(current_chapter=1),
        previous_summary="", unresolved_hooks="")
    if rough is None:
        print("✗ 粗纲生成失败，项目未创建")
        sys.exit(1)

    state = {
        "config": config, "title": title, "blurb": blurb,
        "volume_outline": rough, "setting_library": lib,
        "chapters": [], "summaries": {}, "unresolved_hooks": [],
        "stats": {"outline_revise": 0, "content_revise": 0},
    }
    save_state(state, project_dir)
    # 登记到书架并设为当前书(仅当项目建在 DATA_DIR 下)
    try:
        if os.path.dirname(project_dir) == os.path.abspath(config.DATA_DIR):
            BookshelfManager().mark_opened(os.path.basename(project_dir))
    except Exception:
        pass
    print(f"\n✅ 新书就绪：《{title}》（{project_dir}）")
    print(f"   下一步：python cli.py chapter --project-dir \"{project_dir}\"")


def cmd_chapter(args):
    project_dir = _resolve_project_dir(args.project_dir)
    state = load_state(project_dir)
    config, lib = state["config"], state["setting_library"]
    slm = SettingLibraryManager(lib, config)

    done = {c["num"] for c in state["chapters"]}
    num = args.num if args.num else (max(done) + 1 if done else 1)
    if num in done:
        print(f"✗ 第 {num} 章已存在（已写：{sorted(done)}）")
        sys.exit(1)

    rough = state["volume_outline"]
    chapters = state["chapters"]
    prev_summaries = ("\n".join(f"第{c['num']}章《{c['title']}》：{c.get('summary', '')}"
                                for c in chapters) if chapters else "（这是第一章，无前文）")
    hooks_text = "；".join(state["unresolved_hooks"]) or "（暂无）"
    setting_summary = slm.get_summary(current_chapter=num, unresolved_hooks=state["unresolved_hooks"])

    result = {"chapter": num, "warnings": []}

    # ① 细纲（解析失败或审查 <70 → 带反馈重生成一次）
    outline_agent = DetailedOutlineAgent(config, lib)
    outline, feedback = None, ""
    for attempt in range(2):
        try:
            outline = outline_agent.generate_chapter_outline(
                chapter_number=num, volume_outline=rough,
                setting_summary=setting_summary, previous_chapters_summary=prev_summaries,
                unresolved_hooks=hooks_text, revision_feedback=feedback)
        except Exception as e:
            print(f"  ✗ 细纲异常: {e}")
            outline = None
        if outline is None:
            if attempt == 0:
                print("  ⚠ 细纲失败，重试...")
                continue
            break
        try:
            review = OutlineReviewAgent(config, lib).review(outline, setting_summary, hooks_text)
        except Exception as e:
            print(f"  ⚠ 审查异常（视为通过）: {e}")
            break
        result["outline_review_score"] = review.score
        if review.passed and review.score >= 70:
            print(f"  ✅ 细纲审查通过（{review.score}）")
            break
        print(f"  ⚠ 审查未过（{review.score}），{'带反馈重生成' if attempt == 0 else '保留当前版'}")
        feedback = review_feedback_text(review)
        if attempt == 0:
            state["stats"]["outline_revise"] += 1
    if outline is None:
        print(f"✗ 第 {num} 章细纲生成失败")
        sys.exit(1)
    # 写前守卫的死亡角色警告计入 result
    for v in outline_agent.check_dead_characters(outline):
        result["warnings"].append(v)

    # ② 写作（失败重试一次）
    writer = ChapterWritingAgent(config, lib)
    prev_content = chapters[-1]["content"] if chapters else ""
    content = None
    for attempt in range(2):
        try:
            content = writer.write_chapter(
                chapter_outline=outline, setting_summary=setting_summary,
                previous_content_summary=prev_summaries,
                previous_chapter_content=prev_content)
        except Exception as e:
            print(f"  ✗ 写作异常: {e}")
            content = None
        if content is not None:
            break
        if attempt == 0:
            print("  ⚠ 正文失败，重试...")
    if content is None:
        print(f"✗ 第 {num} 章正文生成失败")
        sys.exit(1)

    # ③ 校验（<70 → 带反馈重写一次）
    try:
        cr = ContentReviewAgent(config, lib).review(content, outline, setting_summary)
        result["content_review_score"] = cr.score
        if (not cr.passed and cr.score < 70):
            print(f"  ⚠ 校验未过（{cr.score}），重写...")
            state["stats"]["content_revise"] += 1
            revised = writer.revise_chapter(
                current_content=content, feedback=review_feedback_text(cr),
                chapter_outline=outline, setting_summary=setting_summary)
            if revised:
                content = revised
                result["revised"] = True
        else:
            print(f"  ✅ 校验通过（{cr.score}）")
    except Exception as e:
        print(f"  ⚠ 校验异常（视为通过）: {e}")

    clean = strip_checklist(content.content)
    print("  摘要生成...")
    try:
        summary = summarize_chapter(num, clean, outline)
    except Exception:
        summary = outline.chapter_objective or ""

    # ④ 维护 + 伏笔登记
    try:
        maint = SettingMaintenanceAgent(config, lib)
        updates = maint.update_from_chapter(content, outline, num)
        for issue in (updates.get("issues") or [])[:5]:
            result["warnings"].append(f"维护矛盾：{issue}")
    except Exception as e:
        print(f"  ⚠ 维护异常（跳过）: {e}")
    for h in (outline.foreshadowing_plant or []):
        if h:
            state["unresolved_hooks"].append(h)
    for h in (outline.foreshadowing_recover or []):
        if h:
            state["unresolved_hooks"] = [u for u in state["unresolved_hooks"] if h not in u and u not in h]

    state["chapters"].append({"num": num, "title": outline.chapter_title,
                              "content": clean, "summary": summary})
    save_state(state, project_dir)

    print(f"\n✅ 第 {num} 章完成：《{outline.chapter_title}》（{len(clean)} 字）")
    print(f"   细纲审查 {result.get('outline_review_score', '-')} 分 | "
          f"正文校验 {result.get('content_review_score', '-')} 分")
    if result["warnings"]:
        print("   ⚠ 一致性警告：")
        for w in result["warnings"]:
            print(f"     · {w}")


def cmd_status(args):
    project_dir = _resolve_project_dir(args.project_dir)
    state = load_state(project_dir)
    lib = state["setting_library"]
    print(f"📖 《{state['title']}》")
    print(f"   题材：{state['config'].genre} | 文风：{state['config'].writing_style} | "
          f"每章目标 {state['config'].words_per_chapter} 字")
    print(f"   简介：{state['blurb'][:60]}…")
    print(f"   已写 {len(state['chapters'])} 章：")
    for c in state["chapters"]:
        print(f"     第{c['num']}章《{c['title']}》 {len(c['content'])}字 — {c.get('summary', '')[:50]}")
    print(f"   设定库：人物 {len(lib.characters)} / 地理 {len(lib.geography)} / 战力 "
          f"{len(lib.power_system)} / 势力 {len(lib.factions)} / 历史 {len(lib.history)}")
    print(f"   未回收伏笔 {len(state['unresolved_hooks'])} 条：")
    for h in state["unresolved_hooks"][:10]:
        print(f"     · {h[:60]}")


def cmd_export(args):
    project_dir = _resolve_project_dir(args.project_dir)
    state = load_state(project_dir)
    out = args.out or os.path.join(project_dir, "manuscript.md")
    parts = [f"# {state['title']}\n", f"> {state['blurb']}\n"]
    for c in state["chapters"]:
        parts.append(f"\n---\n\n## 第{c['num']}章 {c['title']}\n\n{c['content']}\n")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    total = sum(len(c["content"]) for c in state["chapters"])
    print(f"✅ 已导出 {len(state['chapters'])} 章 / {total} 字 → {out}")


def cmd_list(args):
    mgr = BookshelfManager()
    books = mgr.scan()
    if not books:
        print("书架上是空的。开新书:python cli.py new --idea \"<脑洞>\" --project-dir data/projects/<书名>")
        return
    print(format_book_table(books, mgr.registry.get("current", "")))
    cur = mgr.get_current()
    if cur:
        print(f"\n▶ 当前书:《{cur.title}》(目录 {cur.dir_name})")
    else:
        print("\n(未设置当前书,可用 python cli.py use --project-dir <目录> 切换)")


def cmd_use(args):
    mgr = BookshelfManager()
    mgr.scan()
    name = _shelf_dir_name(args.project_dir)
    book = mgr.get_book(name)
    if not book:
        print(f"✗ 书架上没有这本书:{name}")
        print(format_book_table(mgr.books))
        sys.exit(1)
    mgr.mark_opened(name)
    print(f"✅ 当前书已切换为《{book.title}》")
    print(f"   目录:{book.project_dir}")
    print(f"   下一步:python cli.py chapter   (未指定 --project-dir 时自动写当前书)")


# 设定库参数别名:英文内部名 / 中文标签
_LIB_ALIASES = {
    "characters": "characters", "人物": "characters",
    "geography": "geography", "地理": "geography",
    "history": "history", "历史": "history",
    "power_system": "power_system", "战力": "power_system",
    "factions": "factions", "势力": "factions",
}


def cmd_memory(args):
    from setting_library import SettingLibraryManager

    project_dir = _resolve_project_dir(args.project_dir)
    book = read_book(_shelf_dir_name(project_dir))
    if not book:
        print(f"✗ 不是可识别的项目目录:{project_dir}")
        sys.exit(1)

    # ── 伏笔账本 ──
    if args.hooks:
        hooks = read_hooks(book)
        print(f"《{book.title}》未回收伏笔({len(hooks)} 条):")
        for i, h in enumerate(hooks, 1):
            print(f"  [{i}] {h}")
        if not hooks:
            print("  (无)")
        return

    # ── 章节时间线 ──
    if args.timeline:
        timeline = get_chapter_timeline(book)
        print(f"《{book.title}》章节时间线({len(timeline)} 章):")
        for t in timeline:
            print(f"  第{t['num']}章《{t['title']}》 {t['words']}字 — {t['summary'][:60]}")
        if not timeline:
            print("  (还没写过章节)")
        return

    lib = get_setting_library(book)

    # ── 单条目详情 / 单库列表 ──
    if args.lib:
        if lib is None:
            print("✗ 该项目没有设定库数据")
            sys.exit(1)
        slm = SettingLibraryManager(lib, ProjectConfig(project_name=book.title))
        lib_name = _LIB_ALIASES.get(args.lib.strip().lower() if args.lib.isascii() else args.lib.strip())
        if not lib_name:
            print(f"✗ 未知设定库:{args.lib}(可选:{'/'.join(_LIB_ALIASES.keys())})")
            sys.exit(1)
        if args.entry:
            if not slm.entry_exists(lib_name, args.entry):
                print(f"✗ 条目不存在:{args.entry}")
                sys.exit(1)
            print(slm.display_entry(lib_name, args.entry))
            return
        entries = slm.get_entries(lib_name)
        label = slm.get_library_label(lib_name)
        print(f"《{book.title}》{label}({len(entries)} 条):")
        for name, e in entries.items():
            importance = getattr(e, "importance", "?")
            last = getattr(e, "last_active_chapter", None)
            status = getattr(e, "current_status", "") or getattr(e, "basic_info", "")
            tail = f" | 最近活跃第{last}章" if last else ""
            print(f"  · {name} [{importance}]{tail} {str(status)[:40]}")
        return

    # ── 默认:记忆概览 ──
    print(f"📖 《{book.title}》记忆概览")
    counts = book.setting_counts
    print(f"   设定库:人物 {counts.get('characters', 0)} / 地理 {counts.get('geography', 0)} / "
          f"历史 {counts.get('history', 0)} / 战力 {counts.get('power_system', 0)} / "
          f"势力 {counts.get('factions', 0)}")
    timeline = get_chapter_timeline(book)
    if timeline:
        print(f"   最近章节(共 {len(timeline)} 章):")
        for t in timeline[-3:]:
            print(f"     第{t['num']}章《{t['title']}》 — {t['summary'][:60]}")
    else:
        print("   尚未写过章节")
    hooks = read_hooks(book)
    print(f"   未回收伏笔 {len(hooks)} 条" + (f"(查看:--hooks)" if hooks else ""))
    if lib is not None:
        try:
            slm = SettingLibraryManager(lib, ProjectConfig(project_name=book.title))
            issues = slm.check_consistency()
            if issues:
                print(f"   ⚠ 一致性问题 {len(issues)} 条:")
                for issue in issues[:5]:
                    print(f"     · {issue}")
        except Exception:
            pass  # 概览中一致性检查失败不影响其余输出


def main():
    p = argparse.ArgumentParser(description="小说写作流水线 CLI（非交互）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("new", help="创建新书")
    sp.add_argument("--idea", default=DEFAULT_IDEA, help="核心脑洞")
    sp.add_argument("--core-setting", default="")
    sp.add_argument("--name", default="", help="指定书名（默认 AI 生成）")
    sp.add_argument("--genre", default="玄幻")
    sp.add_argument("--style", default="番茄爆款")
    sp.add_argument("--words", type=int, default=3000)
    sp.add_argument("--project-dir", default=os.path.join("data", "projects", "new_book"))

    sp = sub.add_parser("chapter", help="生成一章（默认下一章）")
    sp.add_argument("--num", type=int, default=0)
    sp.add_argument("--project-dir", default="", help="未指定时使用书架当前书")

    sp = sub.add_parser("status", help="项目进度")
    sp.add_argument("--project-dir", default="", help="未指定时使用书架当前书")

    sp = sub.add_parser("export", help="导出全文")
    sp.add_argument("--project-dir", default="", help="未指定时使用书架当前书")
    sp.add_argument("--out", default="")

    sp = sub.add_parser("list", help="列出书架全部书籍")

    sp = sub.add_parser("use", help="切换当前书")
    sp.add_argument("--project-dir", required=True, help="书籍目录(data/projects/ 下)")

    sp = sub.add_parser("memory", help="查看记忆:设定库/时间线/伏笔")
    sp.add_argument("--project-dir", default="", help="未指定时使用书架当前书")
    sp.add_argument("--lib", default="", help="设定库名:characters/geography/history/power_system/factions 或中文")
    sp.add_argument("--entry", default="", help="条目名(配合 --lib 显示详情)")
    sp.add_argument("--hooks", action="store_true", help="未回收伏笔账本")
    sp.add_argument("--timeline", action="store_true", help="章节时间线")

    args = p.parse_args()
    {"new": cmd_new, "chapter": cmd_chapter,
     "status": cmd_status, "export": cmd_export,
     "list": cmd_list, "use": cmd_use, "memory": cmd_memory}[args.cmd](args)


if __name__ == "__main__":
    main()
