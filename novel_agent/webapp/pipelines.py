# -*- coding: utf-8 -*-
"""写作流水线封装 — 复刻 cli.py cmd_new / cmd_chapter 逻辑,插入进度事件钩子

fn(emit) 模式:emit(dict) 由 TaskManager 注入,事件自动带 task_id/ts。
不 print(无终端),所有进度通过事件传出。
"""

import os
import sys


def _engine_path() -> str:
    """引擎根目录(webapp/..),供 sys.path 注入"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


ENGINE_DIR = _engine_path()
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)

import config as engine_config  # noqa: E402

from cli import load_state, save_state  # noqa: E402
from models import ProjectConfig, SettingLibrary  # noqa: E402
from bookshelf import BookshelfManager, _safe_dir_name  # noqa: E402
from setting_library import SettingLibraryManager  # noqa: E402
from rough_outline_agent import RoughOutlineAgent  # noqa: E402
from detailed_outline_agent import DetailedOutlineAgent  # noqa: E402
from outline_review_agent import OutlineReviewAgent  # noqa: E402
from chapter_writing_agent import ChapterWritingAgent  # noqa: E402
from content_review_agent import ContentReviewAgent  # noqa: E402
from setting_maintenance_agent import SettingMaintenanceAgent  # noqa: E402
from write_novel_demo import (  # noqa: E402
    generate_title_and_blurb, summarize_chapter,
    review_feedback_text, strip_checklist,
)


def _data_dir() -> str:
    return engine_config.DATA_DIR


def _stage(emit, stage: str, label: str, detail: str = ""):
    emit({"type": "stage", "stage": stage, "label": label, "detail": detail})


# ═══════════════════════════════════════════════════════════
# 新书:new_book
# ═══════════════════════════════════════════════════════════

def new_book(project_dir: str, idea: str, genre: str, style: str,
             person: str, slang: str, words: int, emit, name: str = "",
             core_setting: str = "") -> dict:
    """三阶段:书名简介 → 设定库 → 粗纲。成功返回 {title, blurb, project_dir}"""
    project_dir = os.path.abspath(project_dir)
    os.makedirs(project_dir, exist_ok=True)
    if os.path.exists(os.path.join(project_dir, "project.json")):
        raise ValueError(f"项目已存在:{project_dir}")

    cfg = ProjectConfig(
        project_name=os.path.basename(project_dir), genre=genre,
        narrative_person=person, writing_style=style,
        internet_slang_level=slang, core_idea=idea,
        core_setting=core_setting, chapters_per_volume=50,
        words_per_chapter=words,
    )

    # ① 书名与简介
    _stage(emit, "title", "构思书名与简介")
    emit({"type": "log", "text": "AI 正在构思书名…"})
    title, blurb = generate_title_and_blurb(cfg)
    if name:
        title = name
    emit({"type": "log", "text": f"书名确定:《{title}》"})

    # ② 设定库
    _stage(emit, "settings", "初始化设定库", "AI 生成 5 大子库")
    lib = SettingLibrary()
    slm = SettingLibraryManager(lib, cfg)
    settings_ok = True
    try:
        slm.generate_initial_settings()
        emit({"type": "log",
              "text": f"设定库就绪:人物 {len(lib.characters)} / 地理 {len(lib.geography)}"
                      f" / 战力 {len(lib.power_system)} / 势力 {len(lib.factions)}"
                      f" / 历史 {len(lib.history)}"})
    except Exception as e:
        settings_ok = False
        emit({"type": "log", "text": f"⚠ 设定库初始化失败(空库继续):{e}"})

    # ③ 粗纲
    _stage(emit, "outline", "生成第一卷粗纲")
    emit({"type": "log", "text": "粗纲 Agent 工作中…"})
    rough = RoughOutlineAgent(cfg, lib).generate_volume_outline(
        volume_number=1, setting_summary=slm.get_summary(current_chapter=1),
        previous_summary="", unresolved_hooks="")
    if rough is None:
        raise RuntimeError("粗纲生成失败")

    state = {
        "config": cfg, "title": title, "blurb": blurb,
        "volume_outline": rough, "setting_library": lib,
        "chapters": [], "summaries": {}, "unresolved_hooks": [],
        "stats": {"outline_revise": 0, "content_revise": 0},
    }
    save_state(state, project_dir)
    # 登记书架(仅当项目建在 DATA_DIR 下)
    try:
        if os.path.dirname(project_dir) == os.path.abspath(_data_dir()):
            BookshelfManager().mark_opened(os.path.basename(project_dir))
    except Exception:
        pass
    return {"title": title, "blurb": blurb, "project_dir": project_dir,
            "settings_ok": settings_ok}


# ═══════════════════════════════════════════════════════════
# 单章:write_chapter
# ═══════════════════════════════════════════════════════════

def write_chapter(project_dir: str, num: int, emit) -> dict:
    """五阶段流水线,复刻 cli.cmd_chapter。返回章节结果 dict"""
    project_dir = os.path.abspath(project_dir)
    state = load_state(project_dir)
    cfg, lib = state["config"], state["setting_library"]
    slm = SettingLibraryManager(lib, cfg)

    done = {c["num"] for c in state["chapters"]}
    if not num:
        num = max(done) + 1 if done else 1
    if num in done:
        raise ValueError(f"第 {num} 章已存在(已写:{sorted(done)})")

    rough = state["volume_outline"]
    chapters = state["chapters"]
    prev_summaries = ("\n".join(f"第{c['num']}章《{c['title']}》:{c.get('summary', '')}"
                                for c in chapters) if chapters else "(这是第一章,无前文)")
    hooks_text = "；".join(state["unresolved_hooks"]) or "（暂无）"
    setting_summary = slm.get_summary(current_chapter=num,
                                      unresolved_hooks=state["unresolved_hooks"])

    result = {"chapter": num, "warnings": []}

    # ① 细纲(解析失败或审查 <70 → 带反馈重生成一次)
    _stage(emit, "outline", f"生成第 {num} 章细纲", "任务式 checklist")
    outline_agent = DetailedOutlineAgent(cfg, lib)
    outline, feedback = None, ""
    for attempt in range(2):
        try:
            outline = outline_agent.generate_chapter_outline(
                chapter_number=num, volume_outline=rough,
                setting_summary=setting_summary,
                previous_chapters_summary=prev_summaries,
                unresolved_hooks=hooks_text, revision_feedback=feedback)
        except Exception as e:
            emit({"type": "log", "text": f"✗ 细纲异常:{e}"})
            outline = None
        if outline is None:
            if attempt == 0:
                emit({"type": "log", "text": "⚠ 细纲失败,重试…"})
                continue
            break
        # ② 小纲审查
        _stage(emit, "outline_review", f"审查第 {num} 章小纲", "8 维度打分")
        try:
            review = OutlineReviewAgent(cfg, lib).review(outline, setting_summary, hooks_text)
        except Exception as e:
            emit({"type": "log", "text": f"⚠ 审查异常(视为通过):{e}"})
            break
        result["outline_review_score"] = review.score
        if review.passed and review.score >= 70:
            emit({"type": "log", "text": f"✅ 细纲审查通过({review.score} 分)"})
            break
        emit({"type": "log",
              "text": f"⚠ 审查未过({review.score}),{'带反馈重生成' if attempt == 0 else '保留当前版'}"})
        feedback = review_feedback_text(review)
        if attempt == 0:
            state["stats"]["outline_revise"] += 1
    if outline is None:
        raise RuntimeError(f"第 {num} 章细纲生成失败")
    # 写前守卫的死亡角色警告计入 result
    for v in outline_agent.check_dead_characters(outline):
        result["warnings"].append(v)

    # ③ 写作(失败重试一次)
    _stage(emit, "writing", f"撰写第 {num} 章正文", f"目标 {cfg.words_per_chapter} 字")
    writer = ChapterWritingAgent(cfg, lib)
    prev_content = chapters[-1]["content"] if chapters else ""
    content = None
    for attempt in range(2):
        try:
            content = writer.write_chapter(
                chapter_outline=outline, setting_summary=setting_summary,
                previous_content_summary=prev_summaries,
                previous_chapter_content=prev_content)
            # Agent 整段返回,完成后推全量文本供前端预览
            emit({"type": "partial", "text": content.content})
        except Exception as e:
            emit({"type": "log", "text": f"✗ 写作异常:{e}"})
            content = None
        if content is not None:
            break
        if attempt == 0:
            emit({"type": "log", "text": "⚠ 正文失败,重试…"})
    if content is None:
        raise RuntimeError(f"第 {num} 章正文生成失败")

    # ④ 校验(<70 → 带反馈重写一次)
    _stage(emit, "content_review", "校验正文", "8 维度匹配度")
    try:
        cr = ContentReviewAgent(cfg, lib).review(content, outline, setting_summary)
        result["content_review_score"] = cr.score
        if (not cr.passed and cr.score < 70):
            emit({"type": "log", "text": f"⚠ 校验未过({cr.score}),重写…"})
            state["stats"]["content_revise"] += 1
            revised = writer.revise_chapter(
                current_content=content, feedback=review_feedback_text(cr),
                chapter_outline=outline, setting_summary=setting_summary)
            if revised:
                content = revised
                result["revised"] = True
                emit({"type": "partial", "text": content.content})
        else:
            emit({"type": "log", "text": f"✅ 校验通过({cr.score} 分)"})
    except Exception as e:
        emit({"type": "log", "text": f"⚠ 校验异常(视为通过):{e}"})

    clean = strip_checklist(content.content)

    # ⑤ 摘要 + 设定维护 + 伏笔登记
    _stage(emit, "maintenance", "设定库维护", "从正文提取设定更新")
    emit({"type": "log", "text": "摘要生成…"})
    try:
        summary = summarize_chapter(num, clean, outline)
    except Exception:
        summary = outline.chapter_objective or ""
    try:
        maint = SettingMaintenanceAgent(cfg, lib)
        updates = maint.update_from_chapter(content, outline, num)
        for issue in (updates.get("issues") or [])[:5]:
            result["warnings"].append(f"维护矛盾:{issue}")
    except Exception as e:
        emit({"type": "log", "text": f"⚠ 维护异常(跳过):{e}"})
    for h in (outline.foreshadowing_plant or []):
        if h:
            state["unresolved_hooks"].append(h)
    for h in (outline.foreshadowing_recover or []):
        if h:
            state["unresolved_hooks"] = [u for u in state["unresolved_hooks"]
                                         if h not in u and u not in h]

    state["chapters"].append({"num": num, "title": outline.chapter_title,
                              "content": clean, "summary": summary})
    save_state(state, project_dir)

    for w in result["warnings"]:
        emit({"type": "warning", "text": w})
    emit({"type": "log",
          "text": f"✅ 第 {num} 章完成:《{outline.chapter_title}》({len(clean)} 字)"})
    result["title"] = outline.chapter_title
    result["words"] = len(clean)
    return result
