# -*- coding: utf-8 -*-
"""
无人值守端到端写作流水线（生产级）
=================================================
取代 write_novel_demo.py。核心改进：
  - 任意脑洞命令行输入（--idea），不再硬编码
  - 全程经 ProjectManager 逐步落盘（原子写），支持 --resume 断点续跑
  - 伏笔台账 ID 化（换皮去重 + LLM 回收映射 + 归档）
  - 确定性一致性校验（死人出场/境界词）写前拦截
  - 字数越界程序化扩写/收缩；校验不过强制重写（最多 2 次），仍不过标记 needs_attention
  - 章节标题悬念式优化 pass
  - 运行审计：每章 JSON 记录 + 汇总 markdown 报告 + 按章漂移评估（多评委盲评）

用法：
  python auto_pipeline.py --name 我的书 --idea "脑洞..." --chapters 50
  python auto_pipeline.py --name 我的书 --resume            # 断点续跑
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as app_config
from models import ProjectConfig, SettingLibrary, VolumeOutline
from api_client import get_client
from utils import save_json, load_json, to_dict, now_str
from project_manager import ProjectManager
from setting_library import SettingLibraryManager
from rough_outline_agent import RoughOutlineAgent
from detailed_outline_agent import DetailedOutlineAgent
from outline_review_agent import OutlineReviewAgent
from chapter_writing_agent import ChapterWritingAgent
from content_review_agent import ContentReviewAgent
from setting_maintenance_agent import SettingMaintenanceAgent
from foreshadow_registry import (ForeshadowRegistry, llm_merge_duplicates,
                                 llm_map_recoveries)
from consistency_checker import run_all_checks, normalize_name
from prompts import TITLE_SYSTEM, get_title_user
from export import export_manuscript, export_fanqie_package, clean_chapter_text

DEFAULT_IDEA = """地球大学生林渊穿越到「魂纹世界」——这个世界的人族在十六岁成年礼上觉醒「魂纹」，
魂纹决定了修炼天赋和战斗方式。林渊觉醒的是一道覆盖全身的「空白魂纹」，被所有人判定为废纹。
但他很快发现，自己的空白魂纹拥有一个逆天能力——拓印：触碰他人的魂纹即可复制其能力。
更可怕的是，他可以把多种魂纹能力组合起来，创造出闻所未闻的战斗方式。"""

# 漂移评估默认采样章（过滤到实际存在的章）
DEFAULT_EVAL_SAMPLES = [1, 2, 3, 10, 20, 35, 50]


def banner(msg):
    print("\n" + "═" * 66)
    print(f"  {msg}")
    print("═" * 66)


def strip_checklist(text: str) -> str:
    marker = "---写作检查---"
    return text.split(marker)[0].strip() if marker in text else (text or "").strip()


def review_feedback_text(result) -> str:
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
# 书名 / 简介 / 章节标题 / 章节摘要
# ═══════════════════════════════════════════════════════════════

def generate_title_and_blurb(cfg: ProjectConfig):
    client = get_client()
    print("  🤖 正在构思书名...")
    titles = []
    for _ in range(3):  # flash 偶发空响应，重试
        raw = client.chat(
            system_prompt="你是网络小说书名创意专家。输出格式：每行一个书名，不加编号、引号或解释。",
            user_prompt=f"""请根据以下小说创作信息，生成 3 个吸引人的网络小说书名。

【故事分类】{cfg.genre}
【核心脑洞】{cfg.core_idea}

网文书名有多种成功风格，请各取一个：
1. **短书名**（2-5字）：简洁霸气，如《斗破苍穹》《遮天》
2. **中书名**（5-10字）：带修饰或标签，如《大奉打更人》《一念永恒》
3. **长书名/轻小说风**（10-25字）：直接交代核心设定和卖点，如《我在精神病院学斩神》《这个勇者明明超强却过分慎重》""",
            temperature=0.9, max_tokens=1500,
        )
        titles = [t.strip().strip('《》""\'\'。， ·') for t in (raw or "").strip().split("\n") if len(t.strip()) >= 2]
        if titles:
            break
    # 番茄读者偏好长书名（卖点直给），取最长的；兜底从脑洞派生
    title = max(titles, key=len) if titles else cfg.core_idea.strip().replace("\n", "")[:12] + "：我的逆天改命"
    print(f"  ✅ 选定书名：《{title}》")

    print("  🤖 正在撰写简介...")
    blurb = client.chat(
        system_prompt="你是番茄小说资深作者，擅长写让读者一眼想点开的作品简介。",
        user_prompt=f"""为核心脑洞为一部{cfg.genre}类网络小说撰写简介，要求：
- 150-250 字
- 开头一句抛出核心冲突或金手指
- 中段用 2-3 个短句堆叠爽点预期（打脸/逆袭/升级）
- 结尾留一个悬念钩子
- 直接输出简介正文，不要标题和解释

【核心脑洞】{cfg.core_idea}""",
        temperature=0.8, max_tokens=500,
    ).strip()
    return title, blurb


_TITLE_STOP = {"初窥门径", "新的开始", "平静的一天", "日常"}


def improve_chapter_title(outline) -> str:
    """悬念式标题 pass：生成 5 个候选，按启发式择优。失败时保留原标题。"""
    original = outline.chapter_title or f"第{outline.chapter_number}章"
    try:
        scenes_summary = "\n".join(
            f"- {s.get('location', '')}：{s.get('summary', '')}"
            for s in outline.scenes if isinstance(s, dict))
        client = get_client()
        result = client.chat_with_json_output(
            TITLE_SYSTEM,
            get_title_user(outline.chapter_number, outline.chapter_objective or "",
                           scenes_summary or "（无场景信息）", original),
            temperature=0.8, max_tokens=2000)
        if "_parse_error" in result:
            return original
        candidates = [normalize_name(c) for c in result.get("candidates", []) if c]
    except Exception:
        return original

    def score(t: str) -> int:
        s = 0
        if 6 <= len(t) <= 14:
            s += 2
        elif 4 <= len(t) <= 18:
            s += 1
        if any(w in t for w in ("谁", "竟", "居然", "为什么", "秘密", "真相", "危机",
                                "来袭", "对决", "反杀", "打脸", "藏", "禁", "破",
                                "杀", "劫", "局", "底牌", "背叛")):
            s += 2
        if t.endswith(("？", "?", "！", "!")):
            s += 1
        if t in _TITLE_STOP:
            s -= 3
        return s

    pool = candidates + [original]
    best = max(pool, key=score)
    return best if best else original


def summarize_chapter(chapter_number: int, content: str, outline) -> str:
    client = get_client()
    summary = client.chat(
        system_prompt="你是小说连载助手，负责为后续章节写作提供前文摘要。",
        user_prompt=f"""用 80-120 字概括第 {chapter_number} 章剧情，必须包含：关键事件、人物状态变化、本章结尾钩子。
直接输出摘要，不要解释。

【本章正文】
{content[:3500]}""",
        temperature=0.3, max_tokens=2000,
    ).strip()
    return summary or outline.chapter_objective or f"第{chapter_number}章剧情推进"


def pacing_constraint_for(pacing_history: list) -> tuple:
    """根据前文章节节奏序列生成本章约束（代码级护栏，模型不守节奏表时兜底）。
    pacing_history: 按章号升序的 pacing_type 列表。返回 (约束文本, 是否硬性)。"""
    recent = [p for p in pacing_history if p]
    if len(recent) >= 2 and recent[-2:] == ["爆发", "爆发"]:
        return ("前两章均为爆发章（连续高强度对决/反转），读者需要喘息。"
                "本章 pacing_type 必须为「缓冲」：以日常互动、关系深化、世界观生活化为主，"
                "禁止大规模战斗与重大反转，结尾只留一个小悬念。", True)
    last4 = recent[-4:]
    if len(last4) >= 4 and "缓冲" not in last4:
        return ("最近 4 章没有任何缓冲章，追读耐力会透支。"
                "强烈建议本章 pacing_type 为「缓冲」。", False)
    return "", False


# ═══════════════════════════════════════════════════════════════
# 流水线主体
# ═══════════════════════════════════════════════════════════════

class AutoPipeline:

    def __init__(self, args):
        self.args = args
        self.pm = ProjectManager(args.name)
        self.state = None
        self.registry = ForeshadowRegistry()
        self.book = {"title": args.name, "blurb": ""}
        self.report = {"project": args.name, "started_at": now_str(),
                       "chapters": {}, "needs_attention": [], "failed": []}

        if args.resume:
            self._load_existing()
        else:
            self._create_new()

        cfg = self.state.config
        self.slm = SettingLibraryManager(self.state.setting_library, cfg)
        self.outline_agent = DetailedOutlineAgent(cfg, self.state.setting_library)
        self.review_agent = OutlineReviewAgent(cfg, self.state.setting_library)
        self.writer = ChapterWritingAgent(cfg, self.state.setting_library)
        self.checker = ContentReviewAgent(cfg, self.state.setting_library)
        self.maintainer = SettingMaintenanceAgent(cfg, self.state.setting_library)

    # ── 项目装载 ───────────────────────────────────────────
    def _project_file(self, name):
        return os.path.join(self.pm.project_dir, name)

    def _load_existing(self):
        state = self.pm.load(self.args.name)
        if state is None:
            print(f"✗ 项目「{self.args.name}」不存在，无法 --resume；去掉 --resume 新建")
            sys.exit(1)
        self.state = state
        self.registry = self.pm.load_registry()
        book = load_json(self._project_file("book.json"))
        if book:
            self.book = book
        report = load_json(self._project_file("pipeline_state.json"))
        if report:
            self.report = report
        print(f"  ▶ 续跑：从第 {state.current_chapter} 章继续（已有 {len(state.chapter_contents)} 章正文）")

    def _create_new(self):
        cfg = ProjectConfig(
            project_name=self.args.name,
            genre=self.args.genre,
            narrative_person=self.args.person,
            writing_style=self.args.style,
            internet_slang_level=self.args.slang,
            core_idea=self.args.idea,
            core_setting=self.args.setting,
            chapters_per_volume=self.args.chapters,
            words_per_chapter=self.args.words,
        )
        self.state = self.pm.create_new(cfg)

    # ── 审计记录 ───────────────────────────────────────────
    def _record(self, num: int, **kwargs):
        rec = self.report["chapters"].get(str(num), {"chapter": num})
        rec.update(kwargs)
        self.report["chapters"][str(num)] = rec
        self.report["updated_at"] = now_str()
        save_json(self.report, self._project_file("pipeline_state.json"))

    def _mark_attention(self, num: int, reason: str):
        if num not in self.report["needs_attention"]:
            self.report["needs_attention"].append(num)
        rec = self.report["chapters"].get(str(num), {"chapter": num})
        reasons = rec.get("attention_reasons", [])
        if reason not in reasons:
            reasons.append(reason)
        rec["attention_reasons"] = reasons
        self.report["chapters"][str(num)] = rec

    # ── 前置阶段 ───────────────────────────────────────────
    def ensure_book(self):
        if self.book.get("blurb"):
            return
        banner("阶段 1 / 书名与简介")
        title, blurb = generate_title_and_blurb(self.state.config)
        self.book = {"title": title, "blurb": blurb, "generated_at": now_str()}
        save_json(self.book, self._project_file("book.json"))

    def ensure_settings(self):
        if any(len(d) for d in (
                self.state.setting_library.characters,
                self.state.setting_library.geography,
                self.state.setting_library.power_system,
                self.state.setting_library.factions,
                self.state.setting_library.history)):
            return
        banner("阶段 2 / 设定库初始化")
        try:
            self.slm.generate_initial_settings()
            lib = self.state.setting_library
            print(f"  ✅ 设定库：人物 {len(lib.characters)} / 地理 {len(lib.geography)}"
                  f" / 战力 {len(lib.power_system)} / 势力 {len(lib.factions)}"
                  f" / 历史 {len(lib.history)}")
        except Exception as e:
            print(f"  ✗ 设定库初始化失败（空库继续）: {e}")
        self.pm.save_setting_library()

    def ensure_rough_outline(self) -> VolumeOutline:
        data = self.state.volume_outlines.get("1")
        if data:
            return data if isinstance(data, VolumeOutline) else VolumeOutline(**data)
        banner("阶段 3 / 第一卷粗纲")
        rough_agent = RoughOutlineAgent(self.state.config, self.state.setting_library)
        rough = rough_agent.generate_volume_outline(
            volume_number=1,
            setting_summary=self.slm.get_summary(current_chapter=1),
            previous_summary="", unresolved_hooks="")
        if rough is None:
            print("  ✗ 粗纲生成失败，流程终止")
            sys.exit(1)
        self.state.volume_outlines["1"] = to_dict(rough)
        self.pm.save_volume_outline(1)
        # 卷级伏笔种子入台账（sticky：跨卷主线，禁模糊回收）
        self.registry.register_many(rough.foreshadowing_planted, chapter=0, sticky=True)
        self.pm.save_registry(self.registry)
        return rough

    # ── 单章流水线 ─────────────────────────────────────────
    def _prev_summaries(self, upto: int) -> str:
        lines = []
        for n in range(1, upto):
            data = self.state.chapter_summaries.get(str(n))
            if isinstance(data, dict) and data.get("summary"):
                lines.append(f"第{n}章《{data.get('title', '')}》：{data['summary']}")
        return "\n".join(lines) if lines else "（这是第一章，无前文）"

    def _prev_content(self, num: int) -> str:
        data = self.state.chapter_contents.get(str(num - 1))
        if isinstance(data, dict):
            return data.get("content", "")
        return ""

    def run_chapter(self, num: int, rough: VolumeOutline):
        t0 = time.time()
        print(f"\n{'─' * 66}\n  ▶ 第 {num}/{self.args.chapters} 章\n{'─' * 66}")
        rec = {"chapter": num, "status": "running"}

        prev_summaries = self._prev_summaries(num)
        hooks_text = self.registry.hooks_text(num)
        setting_summary = self.slm.get_summary(
            current_chapter=num, unresolved_hooks=self.registry.open_texts())

        # 节奏护栏：连续爆发/长期无缓冲时注入硬约束
        pacing_history = []
        for n in range(1, num):
            data = self.state.chapter_outlines.get(str(n))
            pacing_history.append(data.get("pacing_type", "") if isinstance(data, dict) else "")
        pacing_constraint, pacing_hard = pacing_constraint_for(pacing_history)
        if pacing_constraint:
            print(f"  ⏱ 节奏护栏{'（硬性）' if pacing_hard else '（建议）'}：{pacing_constraint[:40]}...")

        # ① 细纲（审查不过 → 带反馈重生成，最多 2 轮）
        outline = None
        feedback = ""
        for attempt in range(2):
            try:
                outline = self.outline_agent.generate_chapter_outline(
                    chapter_number=num, volume_outline=rough,
                    setting_summary=setting_summary,
                    previous_chapters_summary=prev_summaries,
                    unresolved_hooks=hooks_text, revision_feedback=feedback,
                    pacing_constraint=pacing_constraint)
            except Exception as e:
                print(f"  ✗ 细纲生成异常: {e}")
                outline = None
            if outline is None:
                if attempt == 0:
                    continue
                break
            new_title = improve_chapter_title(outline)
            if new_title != outline.chapter_title:
                print(f"  ✏ 标题优化：《{outline.chapter_title}》→《{new_title}》")
                outline.chapter_title = new_title
                rec["title_improved"] = True
            try:
                review = self.review_agent.review(outline, setting_summary, hooks_text)
            except Exception as e:
                print(f"  ✗ 细纲审查异常（标记需关注）: {e}")
                rec["outline_score"] = None
                self._mark_attention(num, f"细纲审查异常: {e}")
                break
            rec["outline_score"] = review.score
            if review.passed:
                break
            print(f"  ⚠ 细纲审查未通过（{review.score} 分），{'带反馈重新生成' if attempt == 0 else '保留当前版本并记录'}")
            feedback = review_feedback_text(review)
            rec["outline_revise"] = True
        if outline is None:
            rec.update(status="failed", reason="细纲生成失败")
            self.report["failed"].append(num)
            self._record(num, **rec)
            return False
        self.state.chapter_outlines[str(num)] = to_dict(outline)
        self.pm.save_chapter_outline(num)
        # 节奏观测：任务数/场景数/节奏类型进入审计记录（验证张弛与任务瘦身效果）
        rec["pacing_type"] = outline.pacing_type or "未标注"
        if pacing_hard and outline.pacing_type != "缓冲":
            rec["pacing_violation"] = True
            self._mark_attention(num, "连续爆发后未按硬约束转为缓冲章")
        rec["task_count"] = sum(len(getattr(outline, f) or []) for f in (
            "character_updates", "foreshadowing_plant", "foreshadowing_recover",
            "hooks_set", "world_building_revealed", "conflicts_advanced"))
        rec["scene_count"] = len(outline.scenes or [])

        # ② 写作
        content = None
        for attempt in range(2):
            try:
                content = self.writer.write_chapter(
                    chapter_outline=outline, setting_summary=setting_summary,
                    previous_content_summary=prev_summaries,
                    previous_chapter_content=self._prev_content(num))
            except Exception as e:
                print(f"  ✗ 写作异常: {e}")
                content = None
            if content is not None:
                break
        if content is None:
            rec.update(status="failed", reason="正文生成失败")
            self.report["failed"].append(num)
            self._record(num, **rec)
            return False

        # ③ 确定性一致性校验（死人出场/境界词）→ 违规则带反馈重写一次
        clean = strip_checklist(content.content)
        checks = run_all_checks(clean, outline, self.state.setting_library)
        rec["violations"] = checks["violations"]
        if not checks["passed"]:
            print("  ⚠ 确定性校验发现 error 级违规，强制修正...")
            try:
                revised = self.writer.revise_chapter(
                    current_content=content,
                    feedback="以下为确定性校验发现的硬性矛盾，必须全部修正（违规角色改为不出场，"
                             "或把场景明确改写为回忆/闪回；未登记境界词改为设定库中的正式境界）：\n"
                             + "\n".join(f"- {v['description']}" for v in checks["violations"]),
                    chapter_outline=outline, setting_summary=setting_summary)
                if revised:
                    content = revised
                    rec["consistency_rewrite"] = True
                    clean = strip_checklist(content.content)
                    checks2 = run_all_checks(clean, outline, self.state.setting_library)
                    rec["violations_after_rewrite"] = checks2["violations"]
                    if not checks2["passed"]:
                        self._mark_attention(num, "确定性校验重写后仍有死人出场违规")
            except Exception as e:
                self._mark_attention(num, f"确定性校验重写失败: {e}")

        # ④ 字数硬控
        try:
            content = self.writer.enforce_word_count(content, outline, setting_summary)
        except Exception as e:
            print(f"  ✗ 字数调整异常（保留原稿）: {e}")
        rec["word_count"] = content.word_count
        rec["revision_count"] = content.revision_count

        # ⑤ 文章校验 → 不过重写（最多 2 次），仍不过标记需关注
        cr = None
        for rewrite in range(3):  # 1 次初审 + 2 次重写后复审
            try:
                cr = self.checker.review(content, outline, setting_summary)
            except Exception as e:
                print(f"  ✗ 校验异常（标记需关注）: {e}")
                self._mark_attention(num, f"文章校验异常: {e}")
                cr = None
                break
            if cr.passed or rewrite == 2:
                break
            print(f"  ⚠ 校验未通过（{cr.score} 分），强制重写（第 {rewrite + 1}/2 次）...")
            rec["content_rewrite"] = rec.get("content_rewrite", 0) + 1
            try:
                revised = self.writer.revise_chapter(
                    current_content=content,
                    feedback=review_feedback_text(cr),
                    chapter_outline=outline, setting_summary=setting_summary)
                if revised:
                    content = revised
                    rec["word_count"] = content.word_count
                    rec["revision_count"] = content.revision_count
                    content = self.writer.enforce_word_count(content, outline, setting_summary)
                    rec["word_count"] = content.word_count
                else:
                    break
            except Exception as e:
                print(f"  ✗ 重写异常: {e}")
                break
        if cr is not None:
            rec["content_score"] = cr.score
            if not cr.passed:
                self._mark_attention(num, f"文章校验重写后仍未通过（{cr.score} 分）")

        # ⑥ 落盘正文 + 摘要（落盘前最后一道字数闸，区间内零开销）
        try:
            content = self.writer.enforce_word_count(content, outline, setting_summary)
            rec["word_count"] = content.word_count
            rec["revision_count"] = content.revision_count
        except Exception as e:
            print(f"  ✗ 最终字数闸异常（保留当前稿）: {e}")
        self.state.chapter_contents[str(num)] = to_dict(content)
        self.pm.save_chapter_content(num, self.state.chapter_contents[str(num)])
        try:
            summary = summarize_chapter(num, strip_checklist(content.content), outline)
        except Exception:
            summary = outline.chapter_objective or ""
        self.state.chapter_summaries[str(num)] = {
            "chapter_number": num, "title": outline.chapter_title,
            "summary": summary, "new_characters": outline.characters_appearing,
            "new_locations": outline.locations, "key_events": [],
            "unresolved_hooks": self.registry.open_texts()}
        self.pm.save_chapter_summaries()

        # ⑦ 伏笔台账更新（入账去重 + 回收映射 + 归档）
        plants = [p for p in (outline.foreshadowing_plant or []) if p and p.strip()]
        res = self.registry.register_many(plants, num)
        try:
            pairs = llm_merge_duplicates(self.registry, res["added"])
            if pairs:
                rec["hook_merges"] = len(pairs)
        except Exception:
            pass
        recovers = [r for r in (outline.foreshadowing_recover or []) if r and r.strip()]
        if recovers:
            try:
                mapped = llm_map_recoveries(self.registry, recovers, num)
                rec["hooks_recovered"] = mapped["recovered"]
                if mapped["unmatched"]:
                    rec["hooks_unmatched_recovers"] = mapped["unmatched"]
            except Exception as e:
                print(f"  ✗ 伏笔回收映射异常（退化为确定性匹配）: {e}")
                for r in recovers:
                    self.registry.recover(r, num)
        archived = self.registry.archive_stale(num)
        if archived:
            rec["hooks_archived"] = archived
        rec["hooks_open"] = len(self.registry.open_items())
        self.pm.save_registry(self.registry)

        # ⑧ 设定库维护
        try:
            self.maintainer.update_from_chapter(content, outline, num)
        except Exception as e:
            print(f"  ✗ 设定库维护异常（跳过）: {e}")
            self._mark_attention(num, f"设定库维护异常: {e}")
        self.pm.save_setting_library()

        # ⑧b 跨库一致性门控：维护后校验悬空引用（势力成员/地理关联/历史关联/道具持有者）
        try:
            lib_issues = self.slm.check_consistency()
            if lib_issues:
                rec["library_consistency_issues"] = lib_issues[:6]
                print(f"  ⚠ 设定库跨库引用问题 ×{len(lib_issues)}：{lib_issues[0]}")
                if len(lib_issues) >= 3:
                    self._mark_attention(num, f"设定库跨库引用问题 ×{len(lib_issues)}")
        except Exception as e:
            print(f"  ✗ 跨库一致性校验异常（跳过）: {e}")

        rec["library_sizes"] = {
            "characters": len(self.state.setting_library.characters),
            "geography": len(self.state.setting_library.geography),
            "factions": len(self.state.setting_library.factions),
            "history": len(self.state.setting_library.history),
            "power_system": len(self.state.setting_library.power_system)}

        # ⑨ 进度落盘
        rec.update(status="done", duration_s=round(time.time() - t0, 1),
                   title=outline.chapter_title)
        if num in self.report["failed"]:
            self.report["failed"].remove(num)  # 重试成功，清理失败标记
        self.state.current_chapter = num + 1
        self.pm.save_meta()
        self._record(num, **rec)
        print(f"  ✅ 第 {num} 章完成：{content.word_count} 字《{outline.chapter_title}》"
              f"（{rec['duration_s']}s）")
        return True

    # ── 收尾 ───────────────────────────────────────────────
    def collect_chapters(self) -> list:
        chapters = []
        for n in sorted(int(k) for k in self.state.chapter_contents):
            content = self.state.chapter_contents[str(n)]
            outline = self.state.chapter_outlines.get(str(n), {})
            title = outline.get("chapter_title") or content.get("title", "")
            chapters.append((n, title, strip_checklist(content.get("content", ""))))
        return chapters

    def refresh_blurb(self):
        """成稿后基于实际内容重写简介，消除"简介承诺正文没有"的期待错位"""
        chapters = self.collect_chapters()
        if len(chapters) < 3:
            return
        first3 = "\n".join(f"第{n}章《{t}》：{txt[:800]}" for n, t, txt in chapters[:3])
        titles = "、".join(f"《{t}》" for _, t, _ in chapters[:10])
        rough = self.state.volume_outlines.get("1", {})
        rough_text = rough.get("narrative_outline", "")[:800] if isinstance(rough, dict) else ""
        try:
            blurb = ""
            for attempt in range(3):  # flash 偶发空响应，重试
                blurb = get_client().chat(
                    system_prompt="你是番茄小说资深作者。简介必须与正文实际内容严格一致，禁止承诺正文没有的情节。",
                    user_prompt=f"""基于以下实际已写成的章节内容，为《{self.book.get('title', '')}》重写简介。要求：
- 150-250 字
- 只承诺正文真实存在的情节与爽点，禁止出现正文没有的桥段
- 开头抛核心冲突/金手指，中段堆叠爽点预期，结尾留钩子
- 直接输出简介正文，不要解释

【第一卷粗纲】{rough_text}

【前三章实际内容节选】
{first3}

【章节标题一览（前10章）】{titles}""",
                    temperature=0.7, max_tokens=4000).strip()
                if blurb:
                    break
            if blurb:
                self.book["blurb"] = blurb
                self.book["blurb_refreshed_at"] = now_str()
                save_json(self.book, self._project_file("book.json"))
                print(f"  ✅ 简介已基于实际内容重写（{len(blurb)} 字）")
            else:
                print("  ⚠ 简介重写连续返回空内容，沿用原简介")
        except Exception as e:
            print(f"  ✗ 简介重写失败（沿用原简介）: {e}")

    def finalize(self):
        banner("阶段 5 / 成品导出")
        self.refresh_blurb()
        chapters = self.collect_chapters()
        paths = export_manuscript(chapters, self.pm.project_dir,
                                  self.book.get("title", self.args.name),
                                  self.book.get("blurb", ""))
        export_fanqie_package(chapters, self.pm.project_dir)
        print(f"  📁 导出：{paths['txt']}\n           {paths['md']}（{paths['total_chars']} 字）")
        self.report["export"] = paths

        if self.args.no_eval:
            self._write_audit_report()
            return

        banner("阶段 6 / 质量评估（多评委盲评）")
        from judge import JudgeClient, evaluate_signing, evaluate_chapter
        judge = JudgeClient(n_judges=3)
        if judge.separate:
            print(f"  评委使用独立模型：{judge.clients[0].model}")

        # 按章漂移曲线
        drift = {}
        available = {n for n, _, _ in chapters}
        for sample in self.args.eval_samples:
            if sample not in available:
                continue
            text = next(t for n, _, t in chapters if n == sample)
            title = next(t for n, t, _ in chapters if n == sample)
            prev_summary = (self.state.chapter_summaries.get(str(sample - 1)) or {}).get("summary", "")
            try:
                r = evaluate_chapter(judge, sample, title, text, prev_summary)
                if "parse_error" not in r:
                    drift[str(sample)] = r
                    print(f"  第 {sample} 章质量：{r.get('total_score')} 分")
            except Exception as e:
                print(f"  第 {sample} 章评估失败: {e}")
        self.report["drift_eval"] = drift

        # 签约评估（前 3 章全文）
        rough = self.state.volume_outlines.get("1", {})
        rough_text = rough.get("narrative_outline", "") if isinstance(rough, dict) else ""
        sign_chapters = [(n, t, x) for n, t, x in chapters if n <= 3]
        if sign_chapters:
            try:
                verdict = evaluate_signing(judge, self.book.get("title", ""),
                                           self.book.get("blurb", ""), sign_chapters, rough_text)
                self.report["sign_eval"] = verdict
                if "parse_error" not in verdict:
                    print(f"  签约评估：{verdict.get('weighted_score', 0)}/100 — {verdict.get('verdict', '')}")
            except Exception as e:
                print(f"  签约评估失败: {e}")

        self._write_audit_report()

    def _write_audit_report(self):
        banner("阶段 7 / 审计报告")
        chapters = self.report["chapters"]
        done = [r for r in chapters.values() if r.get("status") == "done"]
        n = self.args.chapters
        segments = [(1, min(5, n)), (6, min(15, n)), (16, min(30, n)), (31, n)]

        lines = [f"# 长跑审计报告 — {self.args.name}",
                 f"- 生成时间：{now_str()}",
                 f"- 完成章节：{len(done)} / {n}；失败 {len(self.report['failed'])} 章"
                 f"（{self.report['failed'] or '无'}）；需人工关注 {len(self.report['needs_attention'])} 章"
                 f"（{self.report['needs_attention'] or '无'}）",
                 f"- 书名：《{self.book.get('title', '')}》",
                 "",
                 "## 分段统计",
                 "| 章节段 | 章数 | 细纲分均值 | 校验分均值 | 字数均值 | 字数达标率 | 重写率 |",
                 "|---|---|---|---|---|---|---|"]
        target = self.state.config.words_per_chapter
        for lo, hi in segments:
            seg = [r for k, r in chapters.items()
                   if r.get("status") == "done" and lo <= r.get("chapter", 0) <= hi]
            if not seg:
                continue
            o_scores = [r["outline_score"] for r in seg if r.get("outline_score") is not None]
            c_scores = [r["content_score"] for r in seg if r.get("content_score") is not None]
            words = [r.get("word_count", 0) for r in seg]
            in_range = sum(1 for w in words if target * 0.9 <= w <= target * 1.1)
            rewritten = sum(1 for r in seg if r.get("content_rewrite") or r.get("consistency_rewrite"))
            avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else "-"
            lines.append(f"| {lo}-{hi} | {len(seg)} | {avg(o_scores)} | {avg(c_scores)} "
                         f"| {avg(words)} | {in_range}/{len(seg)} | {rewritten}/{len(seg)} |")

        lines += ["", "## 伏笔台账", f"- {self.registry.stats()}",
                  "- 当前 open 伏笔："]
        for f in self.registry.open_items():
            lines.append(f"  - {f.id}（第{f.planted_chapter}章）：{f.text[:60]}")

        lines += ["", "## 确定性违规汇总"]
        viol_counter = {}
        for r in chapters.values():
            for v in (r.get("violations") or []):
                viol_counter[v.get("type", "?")] = viol_counter.get(v.get("type", "?"), 0) + 1
        lines.append(f"- {viol_counter or '无'}")

        drift = self.report.get("drift_eval", {})
        if drift:
            from judge import CHAPTER_DIMS
            dim_keys = [dk for dk, _, _ in CHAPTER_DIMS]
            dim_labels = [label for _, label, _ in CHAPTER_DIMS]
            lines += ["", "## 按章质量漂移（多评委盲评）",
                      "| 章 | 总分 | " + " | ".join(dim_labels) + " |",
                      "|---|---|" + "---|" * len(dim_keys)]
            for k in sorted(drift, key=int):
                r = drift[k]
                dims = r.get("dimensions", {})
                dim_str = " | ".join(str(dims.get(dk, {}).get("score", "-")) for dk in dim_keys)
                lines.append(f"| {k} | {r.get('total_score')} | {dim_str} |")
            scores = [r.get("total_score", 0) for r in drift.values()]
            if scores:
                head = scores[:3]
                lines.append(f"- 首段（前 3 个采样点）均值 {round(sum(head) / len(head), 1)}，"
                             f"末采样点 {scores[-1]}；后段较前段下滑超过 5 分即需排查")

        sign = self.report.get("sign_eval", {})
        if sign and "parse_error" not in sign:
            lines += ["", "## 签约评估（多评委盲评）",
                      f"- 加权总分：{sign.get('weighted_score')}/100；sign_ready: {sign.get('sign_ready')}",
                      f"- 结论：{sign.get('verdict', '')}"]
            for i in sign.get("issues", [])[:5]:
                lines.append(f"  - ⚠ {i}")

        needs = self.report["needs_attention"]
        if needs:
            lines += ["", "## 需人工关注章节明细"]
            for num in needs:
                r = chapters.get(str(num), {})
                lines.append(f"- 第 {num} 章：{r.get('attention_reasons', [])}")

        path = self._project_file("audit_report.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        save_json(self.report, self._project_file("pipeline_state.json"))
        print(f"  📋 审计报告：{path}")


def main():
    parser = argparse.ArgumentParser(description="无人值守小说写作流水线")
    parser.add_argument("--name", required=True, help="项目名（data/projects 下的目录名）")
    parser.add_argument("--idea", default=DEFAULT_IDEA, help="核心脑洞")
    parser.add_argument("--setting", default="", help="核心设定补充")
    parser.add_argument("--genre", default="玄幻", help="故事分类")
    parser.add_argument("--style", default="番茄爆款", help="文风")
    parser.add_argument("--slang", default="中", help="网感程度")
    parser.add_argument("--person", default="第三人称", help="叙事人称")
    parser.add_argument("--chapters", type=int, default=50, help="本章卷章节数")
    parser.add_argument("--words", type=int, default=3000, help="单章目标字数")
    parser.add_argument("--resume", action="store_true", help="断点续跑")
    parser.add_argument("--no-eval", action="store_true", help="跳过最终质量评估（只导出+审计）")
    parser.add_argument("--eval-samples", type=int, nargs="+",
                        default=DEFAULT_EVAL_SAMPLES, help="漂移评估采样章号")
    args = parser.parse_args()

    if not app_config.api_key_configured():
        print("✗ 未配置 DEEPSEEK_API_KEY（环境变量或 novel_agent/.env 文件）")
        sys.exit(1)

    t0 = time.time()
    pipe = AutoPipeline(args)
    pipe.ensure_book()
    pipe.ensure_settings()
    rough = pipe.ensure_rough_outline()

    banner(f"阶段 4 / 逐章写作（{pipe.state.current_chapter} → {args.chapters}）")
    for num in range(pipe.state.current_chapter, args.chapters + 1):
        if str(num) in pipe.state.chapter_contents:
            continue  # 已完成章节（防御性跳过）
        ok = pipe.run_chapter(num, rough)
        if not ok:
            # 断网容忍：等 90 秒后整章重试一次
            print(f"  ⏳ 第 {num} 章失败，等待 90 秒后重试（多为网络中断）...")
            time.sleep(90)
            ok = pipe.run_chapter(num, rough)
        if not ok:
            # 保序终止：跳章会让后续章节"看不见"缺口剧情，接缝必然断裂。
            # 已完成章节均已落盘，网络恢复后 --resume 从本章继续。
            print(f"\n  ✗ 第 {num} 章连续失败，终止本次运行（保持章节顺序）。"
                  f"\n  ✗ 已完成 {len(pipe.state.chapter_contents)} 章已保存；"
                  f"网络恢复后执行：python auto_pipeline.py --name {args.name} --resume")
            sys.exit(2)

    pipe.finalize()
    print(f"\n  总耗时 {round((time.time() - t0) / 60, 1)} 分钟 | "
          f"完成 {len(pipe.state.chapter_contents)} 章 | "
          f"需关注 {len(pipe.report['needs_attention'])} 章 | "
          f"失败 {len(pipe.report['failed'])} 章")


if __name__ == "__main__":
    main()
