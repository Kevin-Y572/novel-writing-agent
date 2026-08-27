# -*- coding: utf-8 -*-
"""
无人值守端到端写作验证 — 番茄小说签约标准
=================================================
完整流程（全程无人工介入）：
  1. AI 书名 + 简介
  2. AI 设定库初始化（5 库）
  3. 第一卷粗纲（叙事化 + 前5章背景释放）
  4. 前 5 章完整单章流水线：
     细纲 → 小纲审查（<70 分带反馈重新生成一次）→ 写作
     → 文章校验（<70 分带反馈重写一次）→ 章节摘要 → 设定库维护
  5. 成品导出（manuscript.md + 过程 JSON）
  6. 番茄签约标准评估（LLM-as-Judge，7 维度加权）
"""

import sys
import os
import json
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import ProjectConfig, SettingLibrary
from api_client import get_client
from setting_library import SettingLibraryManager
from rough_outline_agent import RoughOutlineAgent
from detailed_outline_agent import DetailedOutlineAgent
from outline_review_agent import OutlineReviewAgent
from chapter_writing_agent import ChapterWritingAgent
from content_review_agent import ContentReviewAgent
from setting_maintenance_agent import SettingMaintenanceAgent

# ── 写作配置（README 官方测试脑洞，番茄爆款风） ──────────────
CORE_IDEA = """地球大学生林渊穿越到「魂纹世界」——这个世界的人族在十六岁成年礼上觉醒「魂纹」，
魂纹决定了修炼天赋和战斗方式。林渊觉醒的是一道覆盖全身的「空白魂纹」，被所有人判定为废纹。
但他很快发现，自己的空白魂纹拥有一个逆天能力——拓印：触碰他人的魂纹即可复制其能力。
更可怕的是，他可以把多种魂纹能力组合起来，创造出闻所未闻的战斗方式。"""

CORE_SETTING = "魂纹世界，魂纹分九等，觉醒仪式在魂纹学院进行，觉醒殿是核心场景"
GENRE = "玄幻"
STYLE = "番茄爆款"
N_CHAPTERS = 5
WORDS_PER_CHAPTER = 3000

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "data", "projects", "_auto_fanqie_demo")


def banner(msg):
    print("\n" + "═" * 66)
    print(f"  {msg}")
    print("═" * 66)


def strip_checklist(text: str) -> str:
    marker = "---写作检查---"
    return text.split(marker)[0].strip() if marker in text else (text or "").strip()


def review_feedback_text(result) -> str:
    """把审查结果转为反馈文本（issue 可能是 dict 或 str）"""
    lines = []
    for i in (result.issues or [])[:6]:
        if isinstance(i, dict):
            lines.append(f"[{i.get('severity', '?')}/{i.get('category', '?')}] {i.get('description', '')}")
        else:
            lines.append(str(i))
    for s in (result.suggestions or [])[:4]:
        lines.append(f"建议：{s}")
    return "\n".join(lines) or "（无具体问题，整体质量不达标）"


# ═══════════════════════════════════════════════════════════════
# 第 1 步：书名 + 简介
# ═══════════════════════════════════════════════════════════════

def generate_title_and_blurb(config: ProjectConfig):
    client = get_client()
    print("  🤖 正在构思书名...")
    titles = []
    for _ in range(3):  # flash 偶发空响应，重试
        raw = client.chat(
            system_prompt="你是网络小说书名创意专家。输出格式：每行一个书名，不加编号、引号或解释。",
            user_prompt=f"""请根据以下小说创作信息，生成 3 个吸引人的网络小说书名。

【故事分类】{config.genre}
【核心脑洞】{config.core_idea}

网文书名有多种成功风格，请各取一个：
1. **短书名**（2-5字）：简洁霸气，如《斗破苍穹》《遮天》
2. **中书名**（5-10字）：带修饰或标签，如《大奉打更人》《一念永恒》
3. **长书名/轻小说风**（10-25字）：直接交代核心设定和卖点，如《我在精神病院学斩神》《这个勇者明明超强却过分慎重》""",
            temperature=0.9, max_tokens=300,
        )
        titles = [t.strip().strip('《》""\'\'。， ·') for t in (raw or "").strip().split("\n") if len(t.strip()) >= 2]
        if titles:
            break
    # 番茄读者偏好长书名（卖点直给），取最长的；兜底从脑洞派生而非硬编码
    if titles:
        title = max(titles, key=len)
    else:
        idea_head = config.core_idea.strip().replace("\n", "")[:12]
        title = f"{idea_head}：我的逆天改命"

    print(f"  📖 候选书名：{titles}")
    print(f"  ✅ 选定书名：《{title}》")

    print("  🤖 正在撰写简介...")
    blurb = client.chat(
        system_prompt="你是番茄小说资深作者，擅长写让读者一眼想点开的作品简介。",
        user_prompt=f"""为核心脑洞为一部{config.genre}类网络小说撰写简介，要求：
- 150-250 字
- 开头一句抛出核心冲突或金手指
- 中段用 2-3 个短句堆叠爽点预期（打脸/逆袭/升级）
- 结尾留一个悬念钩子
- 直接输出简介正文，不要标题和解释

【核心脑洞】{config.core_idea}""",
        temperature=0.8, max_tokens=500,
    ).strip()
    print(f"  📄 简介：{blurb[:80]}...")
    return title, blurb


# ═══════════════════════════════════════════════════════════════
# 章节摘要（供下一章前文衔接）
# ═══════════════════════════════════════════════════════════════

def summarize_chapter(chapter_number: int, content: str, outline) -> str:
    client = get_client()
    summary = client.chat(
        system_prompt="你是小说连载助手，负责为后续章节写作提供前文摘要。",
        user_prompt=f"""用 80-120 字概括第 {chapter_number} 章剧情，必须包含：关键事件、人物状态变化、本章结尾钩子。
直接输出摘要，不要解释。

【本章正文】
{content[:3500]}""",
        temperature=0.3, max_tokens=300,
    ).strip()
    if not summary:
        summary = outline.chapter_objective or f"第{chapter_number}章剧情推进"
    return summary


# ═══════════════════════════════════════════════════════════════
# 番茄签约标准评估器
# ═══════════════════════════════════════════════════════════════

FANQIE_DIMS = [
    ("opening_hook", "开篇吸引力", 0.25,
     "黄金三章标准：开篇即冲突，前三章内完成「冲突爆发→金手指觉醒→首次打脸/逆袭兑现」的闭环，第一段能否让读者停留"),
    ("pacing", "爽点密度与节奏", 0.20,
     "每章是否有明确爽点（打脸/升级/获得/逆转），节奏是否明快，是否存在大段无效描写或设定堆砌"),
    ("retention", "追读钩子", 0.15,
     "每章结尾是否有吸引追读的钩子，悬念是否持续在线，章节标题是否有点开欲"),
    ("character", "人设与代入感", 0.15,
     "主角目标是否清晰、行动是否主动，配角功能是否明确，读者代入是否顺畅，有无 OOC"),
    ("genre_fit", "题材与定位", 0.10,
     "玄幻大类标签是否清晰，受众定位是否明确，是否符合番茄平台当前热门方向"),
    ("prose", "文笔与流畅度", 0.10,
     "语言通顺度、错别字/病句/重复用词情况，按网络文学商业化文笔标准评判"),
    ("professionalism", "作品完整度", 0.05,
     "书名点击欲、简介合格度、章节结构完整度"),
]


def evaluate_fanqie_sign(title, blurb, chapters: list, rough_outline_text: str) -> dict:
    """chapters: [(num, title, clean_text)]"""
    client = get_client()
    dims_desc = "\n".join(
        f"{i+1}. **{label}**（权重 {w}）：{desc}"
        for i, (k, label, w, desc) in enumerate(FANQIE_DIMS)
    )
    format_dims = ",\n".join(f'    "{k}": {{"score": 0到100的整数, "comment": "30字内简评"}}'
                             for k, _, _, _ in FANQIE_DIMS)

    # 黄金三章给全文，第 4-5 章给开头
    chapter_blocks = []
    for num, ctitle, text in chapters:
        body = text if num <= 3 else text[:600] + "…（后略）"
        chapter_blocks.append(f"—— 第{num}章《{ctitle}》（{len(text)}字）——\n{body}")
    chapters_text = "\n\n".join(chapter_blocks)

    system_prompt = f"""你是番茄小说（字节跳动旗下免费网文平台）的签约责编，负责评估作品能否通过签约。
你需要基于番茄平台的真实签约标准，对一部 AI 生成的玄幻新作进行严格评估。

【评估维度】：
{dims_desc}

【评分标准】：
- 90-100：品质过硬，签约无悬念
- 80-89：达到签约线，可放心投递
- 70-79：接近签约线，修改后可投
- 60-69：有较大差距，需重写开篇
- 0-59：未达签约标准

【输出格式】：严格 JSON
{{
  "total_score": 82,
  "dimensions": {{
{format_dims}
  }},
  "sign_ready": true/false,
  "verdict": "一句话结论（达到签约线/接近/未达到及原因）",
  "issues": ["最影响签约的问题1", "问题2"],
  "suggestions": ["具体可执行的修改建议1", "建议2"]
}}
注意：dimensions 的 key 必须使用上述英文 key。comment 每条不超过 30 字，issues/suggestions 各最多 5 条。
直接输出 JSON，不要包含 ```json``` 标记。"""

    user_prompt = f"""请评估以下作品能否在番茄小说签约。

【书名】《{title}》
【简介】
{blurb}

【第一卷粗纲梗概】
{rough_outline_text[:1200]}

【章节正文】
{chapters_text}

请按维度逐一严格评估，直接输出 JSON。"""

    print("\n  🧑‍💼 番茄签约责编评估中...")
    result = client.chat_with_json_output(system_prompt, user_prompt,
                                          temperature=0.3, max_tokens=8192)
    if "_parse_error" in result:
        return {"parse_error": True, "raw": result.get("_raw", "")[:2000]}

    # 兼容中文标签 key
    dim_scores = result.get("dimensions", {})
    for k, label, _, _ in FANQIE_DIMS:
        if k not in dim_scores and isinstance(dim_scores.get(label), dict):
            dim_scores[k] = dim_scores.pop(label)

    weighted = 0.0
    for k, label, w, _ in FANQIE_DIMS:
        ds = dim_scores.get(k, {})
        try:
            weighted += float(ds.get("score", 0)) * w
        except (TypeError, ValueError):
            pass
    result["weighted_score"] = round(weighted, 1)
    result["dimensions"] = dim_scores
    return result


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def main():
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)

    banner("第 1 步 / 书名与简介")
    config = ProjectConfig(
        project_name="auto_demo", genre=GENRE, narrative_person="第三人称",
        writing_style=STYLE, internet_slang_level="中",
        core_idea=CORE_IDEA, core_setting=CORE_SETTING,
        chapters_per_volume=50, words_per_chapter=WORDS_PER_CHAPTER,
    )
    title, blurb = generate_title_and_blurb(config)

    banner("第 2 步 / 设定库初始化")
    setting_library = SettingLibrary()
    slm = SettingLibraryManager(setting_library, config)
    try:
        slm.generate_initial_settings()
        print(f"  ✅ 设定库：人物 {len(setting_library.characters)} / 地理 {len(setting_library.geography)}"
              f" / 战力 {len(setting_library.power_system)} / 势力 {len(setting_library.factions)}"
              f" / 历史 {len(setting_library.history)}")
    except Exception as e:
        print(f"  ✗ 设定库初始化失败（空库继续）: {e}")

    banner("第 3 步 / 第一卷粗纲")
    rough_agent = RoughOutlineAgent(config, setting_library)
    rough = None
    try:
        rough = rough_agent.generate_volume_outline(
            volume_number=1, setting_summary=slm.get_summary(current_chapter=1),
            previous_summary="", unresolved_hooks="")
    except Exception as e:
        print(f"  ✗ 粗纲生成失败: {e}")
    if rough is None:
        print("  ✗ 无粗纲，流程终止")
        return

    banner(f"第 4 步 / 前 {N_CHAPTERS} 章完整流水线（细纲→审查→写作→校验→维护）")
    outline_agent = DetailedOutlineAgent(config, setting_library)
    review_agent = OutlineReviewAgent(config, setting_library)
    writer = ChapterWritingAgent(config, setting_library)
    checker = ContentReviewAgent(config, setting_library)
    maintainer = SettingMaintenanceAgent(config, setting_library)

    chapters = []           # [(num, title, clean_text)]
    summaries = {}          # num -> summary
    unresolved_hooks = []   # 未回收伏笔
    stats = {"outline_revise": 0, "content_revise": 0, "failed": 0}

    for num in range(1, N_CHAPTERS + 1):
        print(f"\n{'─' * 66}\n  ▶ 第 {num}/{N_CHAPTERS} 章\n{'─' * 66}")
        # 从已成功章节构建前文摘要（容忍中间章节失败的缺口）
        if num > 1 and chapters:
            prev_summaries = "\n".join(
                f"第{n}章《{t}》：{summaries.get(n, '（摘要缺失）')}"
                for n, t, _ in chapters
            )
        else:
            prev_summaries = "（这是第一章，无前文）"
        hooks_text = "；".join(unresolved_hooks) if unresolved_hooks else "（暂无）"
        setting_summary = slm.get_summary(current_chapter=num, unresolved_hooks=unresolved_hooks)

        # ── 4.1 细纲（解析失败或审查不达标 → 带反馈重生成，最多 2 次） ──
        outline = None
        feedback = ""
        for attempt in range(2):
            try:
                outline = outline_agent.generate_chapter_outline(
                    chapter_number=num, volume_outline=rough,
                    setting_summary=setting_summary,
                    previous_chapters_summary=prev_summaries,
                    unresolved_hooks=hooks_text, revision_feedback=feedback)
            except Exception as e:
                print(f"  ✗ 细纲生成异常: {e}")
                outline = None
            if outline is None:
                if attempt == 0:
                    print("  ⚠ 细纲生成失败，重试一次...")
                    continue
                break
            print(f"  🔍 小纲审查（第 {attempt + 1} 次）...")
            try:
                review = review_agent.review(outline, setting_summary, hooks_text)
            except Exception as e:
                print(f"  ✗ 审查异常（视为通过）: {e}")
                break
            if review.passed and review.score >= 70:
                print(f"  ✅ 细纲审查通过（{review.score} 分）")
                break
            print(f"  ⚠ 细纲审查未通过（{review.score} 分），{'带反馈重新生成' if attempt == 0 else '保留当前版本'}")
            feedback = review_feedback_text(review)
            if attempt == 0:
                stats["outline_revise"] += 1
        if outline is None:
            print(f"  ✗ 第 {num} 章细纲生成失败，跳过本章")
            stats["failed"] += 1
            continue

        # ── 4.2 写作（失败重试一次；+ 校验不达标重写一次） ──
        prev_content = chapters[-1][2] if chapters else ""
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
                print("  ⚠ 正文生成失败，重试一次...")
        if content is None:
            print(f"  ✗ 第 {num} 章正文生成失败，跳过本章")
            stats["failed"] += 1
            continue

        print("  🔍 文章校验...")
        try:
            cr = checker.review(content, outline, setting_summary)
        except Exception as e:
            print(f"  ✗ 校验异常（视为通过）: {e}")
            cr = None
        if cr is not None and not cr.passed and cr.score < 70:
            print(f"  ⚠ 校验未通过（{cr.score} 分），带反馈重写一次...")
            stats["content_revise"] += 1
            try:
                revised = writer.revise_chapter(
                    current_content=content,
                    feedback=review_feedback_text(cr),
                    chapter_outline=outline, setting_summary=setting_summary)
                if revised:
                    content = revised
                    print(f"  ✅ 重写完成（新字数 {content.word_count}）")
            except Exception as e:
                print(f"  ✗ 重写异常，保留原稿: {e}")
        elif cr is not None:
            print(f"  ✅ 校验通过（{cr.score} 分）")

        clean = strip_checklist(content.content)
        chapters.append((num, outline.chapter_title, clean))
        print(f"  ✅ 第 {num} 章完成：{content.word_count} 字《{outline.chapter_title}》")

        # ── 4.3 摘要 + 伏笔登记 ──
        try:
            summaries[num] = summarize_chapter(num, clean, outline)
        except Exception:
            summaries[num] = outline.chapter_objective or ""
        for h in (outline.foreshadowing_plant or []):
            if h:
                unresolved_hooks.append(h)
        for h in (outline.foreshadowing_recover or []):
            if h:
                unresolved_hooks = [u for u in unresolved_hooks
                                    if h not in u and u not in h]

        # ── 4.4 设定库维护 ──
        try:
            maintainer.update_from_chapter(content, outline, num)
        except Exception as e:
            print(f"  ✗ 维护异常（跳过）: {e}")

    banner("第 5 步 / 成品导出")
    manuscript = [f"# {title}\n", f"> {blurb}\n"]
    for num, ctitle, text in chapters:
        manuscript.append(f"\n---\n\n## 第{num}章 {ctitle}\n\n{text}\n")
    md_path = os.path.join(OUT_DIR, "manuscript.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(manuscript))
    meta = {
        "title": title, "blurb": blurb,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "chapters": [{"num": n, "title": t, "words": len(x)} for n, t, x in chapters],
        "summaries": summaries, "unresolved_hooks": unresolved_hooks,
        "stats": stats, "total_minutes": round((time.time() - t0) / 60, 1),
    }
    with open(os.path.join(OUT_DIR, "demo_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(OUT_DIR, "setting_library.json"), "w", encoding="utf-8") as f:
        json.dump({k: {n: e.__dict__ for n, e in v.items()}
                   for k, v in [("characters", setting_library.characters),
                                ("geography", setting_library.geography),
                                ("power_system", setting_library.power_system),
                                ("factions", setting_library.factions),
                                ("history", setting_library.history)]},
                  f, ensure_ascii=False, indent=2, default=str)
    print(f"  📁 成品目录：{OUT_DIR}")
    print(f"     manuscript.md（{sum(len(x) for _, _, x in chapters)} 字正文）")

    banner("第 6 步 / 番茄签约标准评估")
    if not chapters:
        print("  ✗ 无章节可评估")
        return
    try:
        verdict = evaluate_fanqie_sign(title, blurb, chapters,
                                       rough.narrative_outline or "")
    except Exception as e:
        print(f"  ✗ 评估失败: {e}")
        return
    with open(os.path.join(OUT_DIR, "sign_eval.json"), "w", encoding="utf-8") as f:
        json.dump(verdict, f, ensure_ascii=False, indent=2, default=str)

    print("\n╔" + "═" * 62 + "╗")
    print("║  番茄签约评估结果".ljust(60) + "║")
    print("╠" + "═" * 62 + "╣")
    if verdict.get("parse_error"):
        print(f"  ⚠ 评估输出解析失败，原始文本见 sign_eval.json")
    else:
        score = verdict.get("weighted_score", 0)
        icon = "✓" if verdict.get("sign_ready") else "✗"
        print(f"  {icon} 加权总分：{score}/100")
        for k, label, w, _ in FANQIE_DIMS:
            ds = verdict.get("dimensions", {}).get(k, {})
            s = ds.get("score", "-") if isinstance(ds, dict) else "-"
            cmt = ds.get("comment", "") if isinstance(ds, dict) else ""
            print(f"     {label}：{s}  {str(cmt)[:30]}")
        print(f"\n  结论：{verdict.get('verdict', '')}")
        for i in verdict.get("issues", [])[:5]:
            print(f"  ⚠ {i}")
        for s in verdict.get("suggestions", [])[:5]:
            print(f"  → {s}")
    print("╚" + "═" * 62 + "╝")
    print(f"\n  总耗时 {round((time.time() - t0) / 60, 1)} 分钟 | "
          f"细纲重生成 {stats['outline_revise']} 次 | 正文重写 {stats['content_revise']} 次 | "
          f"失败章节 {stats['failed']}")


if __name__ == "__main__":
    main()
