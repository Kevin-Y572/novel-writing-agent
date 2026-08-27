"""
评测 Harness — 对粗纲/细纲/章节正文/设定库管理 四个模块进行系统性评测

架构：
  NovelAgentHarness（主控）
    ├── TestScenario（测试场景定义）
    ├── RoughOutlineEvaluator（粗纲评估）
    ├── DetailedOutlineEvaluator（细纲评估）
    ├── ChapterWritingEvaluator（章节写作评估）
    ├── SettingMaintenanceEvaluator（设定库维护评估）
    ├── CrossAgentChecker（跨 Agent 一致性检查）
    └── HarnessReport（报告生成）

评测方式：LLM-as-Judge（使用同一 DeepSeek API 进行结构化评估）
"""

import sys
import os
import re
import json
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import (
    ProjectConfig, ProjectState, SettingLibrary,
    VolumeOutline, ChapterOutline, ChapterSummary, ChapterContent,
    CharacterEntry, GeographyEntry, HistoryEntry, PowerSystemEntry, FactionEntry,
)
from api_client import get_client
from setting_library import SettingLibraryManager, is_dead, RECALL_CONTEXT_KEYWORDS
from rough_outline_agent import RoughOutlineAgent
from detailed_outline_agent import DetailedOutlineAgent
from chapter_writing_agent import ChapterWritingAgent
from setting_maintenance_agent import SettingMaintenanceAgent
from config import STYLE_PRESETS, INTERNET_SLANG_LEVELS, GENRES


# ═══════════════════════════════════════════════════════════════
# 评估维度 & 评分模型
# ═══════════════════════════════════════════════════════════════

@dataclass
class EvalDimension:
    """单个评估维度"""
    name: str           # 维度名（英文 key）
    label: str          # 维度名（中文）
    weight: float       # 权重（0-1，所有维度权重之和应为 1）
    description: str    # 评估标准描述
    min_score: int = 0
    max_score: int = 100


@dataclass
class EvalResult:
    """单个评估结果"""
    agent_name: str
    passed: bool
    total_score: float          # 0-100 加权总分
    dimension_scores: dict      # {dim_name: {"score": int, "comment": str}}
    issues: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)
    detail: str = ""
    latency_seconds: float = 0.0


# ── 粗纲评估维度 ──────────────────────────────────────────────

ROUGH_OUTLINE_DIMS = [
    EvalDimension("structure_completeness", "结构完整性", 0.15,
                  "是否包含：卷标题、章节范围、叙事化行文脉络、主要冲突、角色弧线、伏笔、关键事件、卷末钩子"),
    EvalDimension("narrative_coherence",  "叙事连贯性", 0.25,
                  "叙事化行文脉络是否逻辑通顺、节奏合理、有起承转合，50章的剧情推进是否有层次感"),
    EvalDimension("idea_alignment",       "脑洞契合度", 0.25,
                  "是否紧密围绕核心脑洞展开，是否充分利用了脑洞中的核心设定"),
    EvalDimension("conflict_quality",     "冲突设计",   0.15,
                  "主要冲突是否有层次（短期/中期/长期），冲突是否推动剧情发展"),
    EvalDimension("foreshadowing_quality","伏笔合理性", 0.10,
                  "伏笔设置是否合理，是否具备可回收性，是否与脑洞相关"),
    EvalDimension("setting_consistency",  "设定一致性", 0.10,
                  "是否与设定库中的世界观、人物、战力体系一致"),
]

# ── 细纲评估维度 ──────────────────────────────────────────────

DETAILED_OUTLINE_DIMS = [
    EvalDimension("task_completeness",    "任务完整性",   0.25,
                  "checklist 是否覆盖：人物更新、伏笔设下/回收、钩子设置、世界观释放、冲突推进"),
    EvalDimension("volume_alignment",     "粗纲对齐",     0.20,
                  "本章细纲是否与卷粗纲中的对应行文脉络段落对齐，是否推进了粗纲规划的剧情"),
    EvalDimension("scene_reasonability",  "场景合理性",   0.15,
                  "场景安排是否合理，字数分配是否恰当，场景之间是否有因果逻辑"),
    EvalDimension("hook_management",      "伏笔钩子处理", 0.15,
                  "伏笔设下是否自然、回收是否合理、结尾钩子是否有吸引力"),
    EvalDimension("character_consistency","人物一致性",   0.15,
                  "出场人物的行为、对话、状态是否与设定库中的人物档案一致"),
    EvalDimension("setting_consistency",  "设定一致性",   0.10,
                  "涉及的世界观信息、战力表现、势力关系是否与设定库一致"),
]

# ── 章节写作评估维度 ──────────────────────────────────────────

CHAPTER_WRITING_DIMS = [
    EvalDimension("outline_adherence",   "细纲遵循度",   0.25,
                  "正文是否完成了细纲 checklist 中的每一项任务（人物更新/伏笔/钩子/世界观/冲突）"),
    EvalDimension("writing_quality",     "写作质量",     0.25,
                  "文笔是否流畅、描写是否生动、节奏是否紧凑、爽点是否到位"),
    EvalDimension("word_count_control",  "字数控制",     0.10,
                  "实际字数是否在目标字数的±10%范围内"),
    EvalDimension("style_match",         "文风匹配",     0.15,
                  "行文风格是否符合指定的文风要求和网感程度"),
    EvalDimension("character_voice",     "人物塑造",     0.15,
                  "人物对话是否符合各自性格，行为是否符合设定，是否有 OOC"),
    EvalDimension("setting_consistency", "设定一致性",   0.10,
                  "是否为未授权的设定添加，战力描写是否与设定库一致"),
]

# ── 设定库维护评估维度 ────────────────────────────────────────

SETTING_MAINTENANCE_DIMS = [
    EvalDimension("coverage",            "覆盖率",       0.25,
                  "是否从正文中提取了所有首次出现的人物/地点/势力/历史事件/战力信息"),
    EvalDimension("accuracy",            "准确性",       0.25,
                  "提取的信息是否与正文内容一致，是否有误读或虚构"),
    EvalDimension("no_redundancy",       "无冗余",       0.15,
                  "是否避免了重复添加已有条目，是否避免了无变化条目的无效更新"),
    EvalDimension("consistency",         "一致性",       0.15,
                  "新条目与已有设定库是否自洽，是否检查了矛盾"),
    EvalDimension("importance_accuracy", "重要性标注",   0.10,
                  "core/supporting/minor 的标注是否合理"),
    EvalDimension("window_update",       "活跃窗口更新", 0.10,
                  "last_active_chapter 是否正确更新，出场人物/地点的时间戳是否刷新"),
]


# ═══════════════════════════════════════════════════════════════
# TestScenario — 测试场景定义
# ═══════════════════════════════════════════════════════════════

@dataclass
class TestScenario:
    """一个测试场景"""
    id: str
    name: str
    description: str
    core_idea: str                    # 核心脑洞
    core_setting: str = ""            # 核心设定补充
    genre: str = "玄幻"
    writing_style: str = "番茄爆款"
    narrative_person: str = "第三人称"
    internet_slang_level: str = "中"
    words_per_chapter: int = 3000
    author_notes: str = ""
    # 预置设定库（如果提供，跳过 Phase 1 的 AI 初始化）
    preset_settings: Optional[SettingLibrary] = None
    # 评估范围：哪些 Agent 需要评估
    eval_agents: tuple = ("rough_outline", "detailed_outline", "chapter_writing", "setting_maintenance")


# ── 内置测试场景 ──────────────────────────────────────────────

BUILTIN_SCENARIOS = [
    TestScenario(
        id="scenario_01",
        name="魂纹世界（经典穿越）",
        description="测试 Agent 对经典穿越+修炼体系脑洞的处理能力",
        core_idea="""
主角林渊穿越到一个以「魂纹」为力量体系的世界。
十六岁觉醒魂纹，决定修炼天赋。林渊觉醒的是罕见的「空白魂纹」，
被判定为废纹，遭受嘲笑。但他发现空白魂纹有拓印能力——可以拓印万物魂纹为己用。
从此踏上逆袭之路。
""",
        core_setting="魂纹世界，魂纹分九等，觉醒仪式在魂纹学院进行，觉醒殿是核心场景",
        genre="玄幻",
        writing_style="番茄爆款",
        words_per_chapter=3000,
    ),
    TestScenario(
        id="scenario_02",
        name="深渊副本（系统流）",
        description="测试 Agent 对系统流+游戏化设定的处理能力",
        core_idea="""
2025年，全球出现深渊副本，人类可以觉醒职业进入副本战斗。
主角苏尘觉醒的是「时间操控者」职业，可以操控副本内的时间流速。
在副本中，他发现了深渊背后的真相——这是一场宇宙级文明的试炼。
""",
        core_setting="现代都市+副本世界双线，职业系统类似MMORPG，副本有等级和难度评级",
        genre="都市",
        writing_style="热血战斗",
        words_per_chapter=3000,
    ),
    TestScenario(
        id="scenario_03",
        name="帝国权谋（重生复仇）",
        description="测试 Agent 对重生+权谋+多势力关系的处理能力",
        core_idea="""
大燕国太子楚云寒，在登基前夕被亲弟弟和未婚妻联手毒杀。
重生回到三年前，他带着前世记忆，开始布局复仇。
但他逐渐发现，前世的背叛背后，隐藏着更大的阴谋。
""",
        core_setting="架空古代帝国，朝堂+江湖双线，势力包括皇族、三大世家、暗卫、江湖门派",
        genre="历史",
        writing_style="悬疑烧脑",
        words_per_chapter=3000,
    ),
]


# ═══════════════════════════════════════════════════════════════
# LLM-as-Judge 评估器基类
# ═══════════════════════════════════════════════════════════════

class BaseEvaluator:
    """LLM-as-Judge 评估器基类"""

    def __init__(self, dimensions: list[EvalDimension], agent_name: str):
        self.dimensions = dimensions
        self.agent_name = agent_name

    def _build_eval_prompt(self, input_data: dict, output_data: dict) -> str:
        """子类实现：构建评估 prompt"""
        raise NotImplementedError

    def evaluate(self, input_data: dict, output_data: dict) -> EvalResult:
        """
        执行 LLM-as-Judge 评估。

        Args:
            input_data: 输入给 Agent 的数据
            output_data: Agent 的输出数据

        Returns:
            EvalResult: 结构化评估结果
        """
        print(f"\n  🔍 正在评估 {self.agent_name}（{len(self.dimensions)} 个维度）...")

        start = time.time()
        user_prompt = self._build_eval_prompt(input_data, output_data)

        # 维度说明
        dims_desc = "\n".join(
            f"{i+1}. **{d.label}**（权重 {d.weight}）：{d.description}"
            for i, d in enumerate(self.dimensions)
        )
        # 输出格式中显式列出每个维度的英文 key，避免 LLM 用中文标签作 key
        format_dims = ",\n".join(
            f'    "{d.name}": {{"score": 0到100的整数, "comment": "30字内简评"}}'
            for d in self.dimensions
        )

        system_prompt = f"""你是小说创作质量评估专家。你需要对 AI 生成的小说内容进行结构化评估。

【评估维度】：
{dims_desc}

【评分标准】：
- 90-100：优秀，完全达到标准
- 80-89：良好，有少量可改进之处
- 70-79：一般，有明显不足
- 60-69：较差，存在较多问题
- 0-59：不合格，需要重做

【输出格式】：严格 JSON
{{
  "total_score": 85,
  "dimensions": {{
{format_dims}
  }},
  "issues": ["问题1", "问题2"],
  "suggestions": ["建议1", "建议2"],
  "detail": "整体评价概要"
}}
注意：dimensions 中的 key 必须使用上述英文 key（如 "{self.dimensions[0].name}"），不能用中文标签。
comment 每条不超过 30 字，issues/suggestions 每条不超过 30 字且最多 5 条，detail 不超过 100 字。
直接输出 JSON，不要包含 ```json``` 标记。"""

        client = get_client()
        max_attempts = 3
        result = None
        last_error = "未执行评估"

        for _ in range(max_attempts):
            try:
                result = client.chat_with_json_output(
                    system_prompt, user_prompt,
                    temperature=0.3, max_tokens=8192,
                )
            except Exception as e:
                last_error = f"LLM-as-Judge 调用失败: {e}"
                result = None
                continue

            if "_parse_error" in result:
                last_error = "输出解析为 JSON 失败"
                result = None
                continue

            # 校验维度完整性：缺失或格式损坏的维度视为本次评估失败，触发重试
            dim_scores = result.get("dimensions", {})
            # 兼容 LLM 用中文标签作 key 的情况，映射回英文 key
            for d in self.dimensions:
                if d.name not in dim_scores and isinstance(dim_scores.get(d.label), dict):
                    dim_scores[d.name] = dim_scores.pop(d.label)
            missing = []
            for dim in self.dimensions:
                ds = dim_scores.get(dim.name)
                if not isinstance(ds, dict):
                    missing.append(dim.name)
                    continue
                try:
                    ds["score"] = float(ds["score"])
                except (KeyError, TypeError, ValueError):
                    missing.append(dim.name)
            if missing:
                last_error = f"评估结果缺少/损坏维度: {missing}"
                result = None
                continue
            break

        if result is None:
            return EvalResult(
                agent_name=self.agent_name,
                passed=False,
                total_score=0,
                dimension_scores={},
                issues=[f"LLM-as-Judge 评估失败（已重试 {max_attempts} 次）：{last_error}"],
                latency_seconds=round(time.time() - start, 1),
            )

        # 加权总分自行计算；passed 由加权分判定，不采信 LLM 自报字段
        weighted = sum(dim_scores[d.name]["score"] * d.weight for d in self.dimensions)

        return EvalResult(
            agent_name=self.agent_name,
            passed=weighted >= 70,
            total_score=round(weighted, 1),
            dimension_scores=dim_scores,
            issues=result.get("issues", []),
            suggestions=result.get("suggestions", []),
            detail=result.get("detail", ""),
            latency_seconds=round(time.time() - start, 1),
        )


# ═══════════════════════════════════════════════════════════════
# 四个专用评估器
# ═══════════════════════════════════════════════════════════════

class RoughOutlineEvaluator(BaseEvaluator):
    """粗纲生成评估器"""

    def __init__(self):
        super().__init__(ROUGH_OUTLINE_DIMS, "粗纲生成 Agent")

    def _build_eval_prompt(self, input_data: dict, output_data: dict) -> str:
        return f"""请评估以下粗纲生成质量。

【输入 — 核心脑洞】
{input_data.get("core_idea", "")}

【输入 — 核心设定补充】
{input_data.get("core_setting", "（无）")}

【输入 — 故事分类】
{input_data.get("genre", "玄幻")}

【输入 — 设定库概要】
{input_data.get("setting_summary", "（空）")}

【输出 — AI 生成的粗纲】
{output_data.get("outline_text", output_data.get("raw", ""))[:6000]}

请按维度逐一评估，直接输出 JSON。"""


class DetailedOutlineEvaluator(BaseEvaluator):
    """细纲生成评估器"""

    def __init__(self):
        super().__init__(DETAILED_OUTLINE_DIMS, "细纲生成 Agent")

    def _build_eval_prompt(self, input_data: dict, output_data: dict) -> str:
        return f"""请评估以下细纲生成质量。

【输入 — 粗纲概要】
{input_data.get("volume_outline_summary", "")[:2000]}

【输入 — 设定库概要】
{input_data.get("setting_summary", "")[:2000]}

【输入 — 前文摘要】
{input_data.get("previous_summary", "（第一章）")}

【输出 — AI 生成的细纲】
{output_data.get("outline_json", output_data.get("raw", ""))[:6000]}

请按维度逐一评估，直接输出 JSON。"""


class ChapterWritingEvaluator(BaseEvaluator):
    """章节写作评估器"""

    def __init__(self):
        super().__init__(CHAPTER_WRITING_DIMS, "章节写作 Agent")

    def _build_eval_prompt(self, input_data: dict, output_data: dict) -> str:
        return f"""请评估以下章节写作质量。

【输入 — 细纲（任务书）】
{input_data.get("outline_json", "")[:3000]}

【输入 — 设定库概要】
{input_data.get("setting_summary", "")[:2000]}

【输入 — 写作配置】
文风：{input_data.get("writing_style", "")}
网感：{input_data.get("internet_slang_level", "")}
人称：{input_data.get("narrative_person", "")}
目标字数：{input_data.get("words_per_chapter", 3000)}

【输出 — AI 生成的正文】
{output_data.get("content", output_data.get("raw", ""))[:6000]}

【实际字数】{output_data.get("word_count", "未知")}

请按维度逐一评估，直接输出 JSON。"""


class SettingMaintenanceEvaluator(BaseEvaluator):
    """设定库维护评估器"""

    def __init__(self):
        super().__init__(SETTING_MAINTENANCE_DIMS, "设定库维护 Agent")

    def _build_eval_prompt(self, input_data: dict, output_data: dict) -> str:
        return f"""请评估以下设定库维护质量。

【输入 — 章节正文（前部分）】
{input_data.get("chapter_content", "")[:4000]}

【输入 — 章节细纲】
{input_data.get("outline_json", "")[:3000]}

【输入 — 维护前设定库概要】
{input_data.get("settings_before", "")[:2000]}

【输出 — 维护后设定库概要】
{output_data.get("settings_after", "")[:2000]}

【输出 — 新增条目数】
{output_data.get("new_entries", 0)}

【输出 — 更新条目数】
{output_data.get("updates", 0)}

请按维度逐一评估，直接输出 JSON。"""


# ═══════════════════════════════════════════════════════════════
# CrossAgentChecker — 跨 Agent 一致性检查
# ═══════════════════════════════════════════════════════════════

@dataclass
class CrossAgentCheckResult:
    """跨 Agent 一致性检查结果"""
    passed: bool
    checks: list   # [{"name": "...", "passed": bool, "detail": "..."}]
    issues: list
    score: int     # 0-100


class CrossAgentChecker:
    """检查 Agent 之间的数据流转一致性（不调 API，纯本地逻辑）"""

    @staticmethod
    def _bigrams(text: str) -> set:
        """提取文本的字符 2-gram 集合（去除空白与标点，无分词依赖即可匹配中文）"""
        cleaned = "".join(ch for ch in text if ch.isalnum())
        if len(cleaned) < 2:
            return {cleaned} if cleaned else set()
        return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}

    @staticmethod
    def _name_variants(raw: str) -> list:
        """生成条目名匹配变体：剥离括号注释、按顿号/间隔号拆分。
        LLM 常把出场人物写成「林峰（堂兄）」「青岚城·林家宗祠广场」甚至整串列表"""
        if not raw:
            return []
        base = re.sub(r"[（(].*?[)）]", "", raw).strip()
        variants = {base} if len(base) >= 2 else set()
        for part in re.split(r"[、，,·/]", base):
            part = part.strip()
            if len(part) >= 2:
                variants.add(part)
        return sorted(variants)

    def check_chain(
        self,
        volume_outline: VolumeOutline,
        chapter_outline: ChapterOutline,
        chapter_content: ChapterContent,
        setting_library: SettingLibrary,
        updates_summary: dict,
        target_words: int = 3000,
    ) -> CrossAgentCheckResult:
        """检查完整流水线的一致性"""
        checks = []
        issues = []

        # ── 检查 1: 粗纲 → 细纲对齐 ──
        checks.append(self._check_volume_to_chapter(volume_outline, chapter_outline, issues))

        # ── 检查 2: 细纲 → 正文对齐（任务完成度） ──
        checks.append(self._check_outline_to_content(chapter_outline, chapter_content, issues))

        # ── 检查 3: 正文 → 设定库更新（覆盖率） ──
        checks.append(self._check_content_to_settings(
            chapter_outline, chapter_content, setting_library, updates_summary, issues,
        ))

        # ── 检查 4: 细纲 → 粗纲伏笔衔接 ──
        checks.append(self._check_hooks_flow(volume_outline, chapter_outline, issues))

        # ── 检查 5: 正文 → 细纲字数匹配 ──
        checks.append(self._check_word_count(chapter_outline, chapter_content, issues, target_words))

        # ── 检查 6: 设定库自洽 ──
        checks.append(self._check_setting_self_consistency(setting_library, issues))

        # ── 检查 7: 死亡角色守卫（前后情节一致性） ──
        checks.append(self._check_dead_revival(setting_library, chapter_content, issues))

        all_passed = all(c["passed"] for c in checks)
        score = sum(1 for c in checks if c["passed"]) / len(checks) * 100

        return CrossAgentCheckResult(
            passed=all_passed,
            checks=checks,
            issues=issues,
            score=round(score),
        )

    def _check_volume_to_chapter(
        self, volume: VolumeOutline, chapter: ChapterOutline, issues: list
    ) -> dict:
        """检查细纲是否呼应粗纲"""
        vol_ref = chapter.volume_reference
        narrative = volume.narrative_outline

        if not vol_ref:
            return {"name": "粗纲→细纲对齐", "passed": True, "detail": "无 volume_reference 字段"}

        # 中文无空白分词，原 split() 匹配失效；改用 2-gram 重合度判断关联
        overlap = self._bigrams(vol_ref) & self._bigrams(narrative)

        if len(overlap) >= 2:
            return {"name": "粗纲→细纲对齐", "passed": True,
                    "detail": f"volume_reference 与粗纲有 {len(overlap)} 个 2-gram 重合"}
        else:
            issues.append(f"细纲 volume_reference「{vol_ref}」与粗纲无明显关联")
            return {"name": "粗纲→细纲对齐", "passed": False,
                    "detail": "volume_reference 与粗纲无明显关联"}

    def _check_outline_to_content(
        self, chapter: ChapterOutline, content: ChapterContent, issues: list
    ) -> dict:
        """检查正文是否覆盖细纲任务"""
        content_text = content.content
        missed = []

        # 检查关键人物是否出场（名字可能带括号注释或多角色整串，取变体匹配）
        for char in chapter.characters_appearing:
            if char and not any(v in content_text for v in self._name_variants(char)):
                missed.append(f"人物「{char}」未在正文中出场")

        # 检查关键地点是否出现
        for loc in chapter.locations:
            if loc and not any(v in content_text for v in self._name_variants(loc)):
                missed.append(f"地点「{loc}」未在正文中出现")

        # 检查钩子是否设置：以 2-gram 重合度衡量钩子是否在正文中体现
        # （钩子是"XX似乎有异动"这类描述，正文会换措辞，无法精确匹配）
        for hook in chapter.hooks_set:
            if not hook:
                continue
            grams = self._bigrams(hook)
            if not grams:
                continue
            min_matched = max(1, len(grams) // 3)
            matched = sum(1 for g in grams if g in content_text)
            if matched < min_matched:
                missed.append(f"钩子「{hook}」可能未在正文中体现")

        if not missed:
            return {"name": "细纲→正文任务覆盖", "passed": True,
                    "detail": f"所有 {len(chapter.characters_appearing)} 个人物 + {len(chapter.locations)} 个地点均已覆盖"}
        else:
            issues.extend(missed)
            return {"name": "细纲→正文任务覆盖", "passed": False,
                    "detail": f"有 {len(missed)} 项未覆盖：{'; '.join(missed[:3])}"}

    def _check_content_to_settings(
        self, chapter: ChapterOutline, content: ChapterContent,
        setting_library: SettingLibrary, updates: dict, issues: list
    ) -> dict:
        """检查正文中的新设定是否入库"""
        new_entries = updates.get("new_entries", 0)
        update_count = updates.get("updates", 0)

        # 检查细纲中的人物是否已入库（同样取名字变体匹配，忽略括号注释）
        missing = []
        for char in chapter.characters_appearing:
            if not char:
                continue
            variants = self._name_variants(char)
            if variants and not any(v in setting_library.characters for v in variants):
                missing.append(char)

        if missing:
            issues.append(f"人物 {missing} 细纲中出场但未入库")
            return {"name": "正文→设定库更新", "passed": False,
                    "detail": f"有 {len(missing)} 个人物未入库：{missing}"}
        else:
            return {"name": "正文→设定库更新", "passed": True,
                    "detail": f"新增 {new_entries} + 更新 {update_count} 条，所有出场人物均已入库"}

    def _foreshadow_covered(self, vol_hook: str, chapter_hooks: list) -> bool:
        """粗纲伏笔是否被任一细纲伏笔体现（包含关系或 2-gram 重合；粗纲与细纲措辞层级不同，精确匹配必然误判）"""
        vol_grams = self._bigrams(vol_hook)
        for ch_hook in chapter_hooks:
            if vol_hook in ch_hook or ch_hook in vol_hook:
                return True
            if len(vol_grams & self._bigrams(ch_hook)) >= 2:
                return True
        return False

    def _check_hooks_flow(
        self, volume: VolumeOutline, chapter: ChapterOutline, issues: list
    ) -> dict:
        """检查伏笔流"""
        vol_planted = [h for h in volume.foreshadowing_planted if h]
        ch_hooks = [h for h in (chapter.foreshadowing_plant + chapter.foreshadowing_recover) if h]

        # 检查粗纲伏笔是否在细纲中体现（模糊匹配，设下与回收都算体现）
        covered = [h for h in vol_planted if self._foreshadow_covered(h, ch_hooks)]
        uncovered = [h for h in vol_planted if h not in covered]
        if vol_planted and not covered:
            issues.append(f"粗纲伏笔 {uncovered} 均未在细纲中体现")

        return {"name": "伏笔流衔接", "passed": bool(covered) or not vol_planted,
                "detail": f"粗纲伏笔 {len(vol_planted)} 个 → 细纲体现 {len(covered)} 个"}

    def _check_dead_revival(
        self, setting_library: SettingLibrary, content: ChapterContent, issues: list
    ) -> dict:
        """前后情节一致性：已死亡/退场角色不得在正文中出场（回忆/闪回语境除外）"""
        text = content.content or ""
        dead_total, hits = 0, []
        for name, entry in setting_library.characters.items():
            if not name or len(name) < 2:
                continue
            status = getattr(entry, "current_status", "") or ""
            if not is_dead(status):
                continue
            dead_total += 1
            start = 0
            while True:
                idx = text.find(name, start)
                if idx < 0:
                    break
                window = text[max(0, idx - 40): idx + len(name) + 40]
                if not any(k in window for k in RECALL_CONTEXT_KEYWORDS):
                    hits.append(name)
                    break
                start = idx + len(name)
        if hits:
            issues.append(f"已死亡/退场角色 {hits} 在正文中出场（非回忆语境）")
            return {"name": "死亡角色守卫", "passed": False,
                    "detail": f"{hits} 疑似死亡后复活出场"}
        return {"name": "死亡角色守卫", "passed": True,
                "detail": f"已退场角色 {dead_total} 个，无违规出场" if dead_total else "无已死亡角色"}

    def _check_word_count(
        self, chapter: ChapterOutline, content: ChapterContent, issues: list,
        target_words: int = 3000,
    ) -> dict:
        """检查字数（容差与章节写作评估维度一致：±10%）"""
        actual = content.word_count
        if actual == 0:
            issues.append("正文字数为 0（未生成或统计失败）")
            return {"name": "字数检查", "passed": False, "detail": "字数为 0（未生成）"}

        diff_pct = abs(actual - target_words) / target_words * 100
        passed = diff_pct <= 10

        if not passed:
            issues.append(f"字数偏差 {diff_pct:.1f}%（实际 {actual} / 目标 {target_words}）")

        return {"name": "字数检查", "passed": passed,
                "detail": f"实际 {actual} / 目标 {target_words}（偏差 {diff_pct:.1f}%）"}

    def _check_setting_self_consistency(
        self, setting_library: SettingLibrary, issues: list
    ) -> dict:
        """检查设定库内部一致性"""
        # 调用已有的 check_consistency 逻辑
        slm = SettingLibraryManager(setting_library, ProjectConfig())
        local_issues = slm.check_consistency()

        if local_issues:
            issues.extend(local_issues)
            return {"name": "设定库自洽", "passed": False,
                    "detail": f"发现 {len(local_issues)} 个矛盾"}
        else:
            return {"name": "设定库自洽", "passed": True,
                    "detail": "未发现内部矛盾"}


# ═══════════════════════════════════════════════════════════════
# HarnessReport — 报告生成
# ═══════════════════════════════════════════════════════════════

@dataclass
class HarnessReport:
    """评测报告"""
    scenario_id: str
    scenario_name: str
    timestamp: str
    # 各 Agent 评估结果
    rough_outline_eval: Optional[EvalResult] = None
    detailed_outline_eval: Optional[EvalResult] = None
    chapter_writing_eval: Optional[EvalResult] = None
    setting_maintenance_eval: Optional[EvalResult] = None
    # 跨 Agent 检查
    cross_agent_check: Optional[CrossAgentCheckResult] = None
    # 汇总
    overall_score: float = 0.0
    overall_passed: bool = False
    total_latency_seconds: float = 0.0

    def to_dict(self) -> dict:
        """转为可序列化 dict"""
        def eval_to_dict(e: Optional[EvalResult]) -> Optional[dict]:
            if e is None:
                return None
            return {
                "agent_name": e.agent_name,
                "passed": e.passed,
                "total_score": e.total_score,
                "dimension_scores": e.dimension_scores,
                "issues": e.issues,
                "suggestions": e.suggestions,
                "detail": e.detail,
                "latency_seconds": e.latency_seconds,
            }

        return {
            "scenario_id": self.scenario_id,
            "scenario_name": self.scenario_name,
            "timestamp": self.timestamp,
            "overall_score": self.overall_score,
            "overall_passed": self.overall_passed,
            "total_latency_seconds": self.total_latency_seconds,
            "evaluations": {
                "rough_outline": eval_to_dict(self.rough_outline_eval),
                "detailed_outline": eval_to_dict(self.detailed_outline_eval),
                "chapter_writing": eval_to_dict(self.chapter_writing_eval),
                "setting_maintenance": eval_to_dict(self.setting_maintenance_eval),
            },
            "cross_agent_check": {
                "passed": self.cross_agent_check.passed,
                "score": self.cross_agent_check.score,
                "checks": self.cross_agent_check.checks,
                "issues": self.cross_agent_check.issues,
            } if self.cross_agent_check else None,
        }

    def print_console(self):
        """打印到控制台"""
        W = 65
        print()
        print("╔" + "═" * W + "╗")
        print(f"║  评测报告：{self.scenario_name:<{W-9}}║")
        print(f"║  时间：{self.timestamp:<{W-8}}║")
        print("╠" + "═" * W + "╣")

        # Agent 评估
        evals = [
            ("粗纲生成", self.rough_outline_eval),
            ("细纲生成", self.detailed_outline_eval),
            ("章节写作", self.chapter_writing_eval),
            ("设定库维护", self.setting_maintenance_eval),
        ]

        for label, e in evals:
            if e is None:
                print(f"║  {label}：未评估" + " " * (W - len(label) - 8) + "║")
                continue
            icon = "✓" if e.passed else "✗"
            dims_str = ""
            if e.dimension_scores:
                top_dims = []
                for dim in (ROUGH_OUTLINE_DIMS if "粗纲" in label else
                            DETAILED_OUTLINE_DIMS if "细纲" in label else
                            CHAPTER_WRITING_DIMS if "章节" in label else
                            SETTING_MAINTENANCE_DIMS):
                    ds = e.dimension_scores.get(dim.name, {})
                    s = ds.get("score", "-") if isinstance(ds, dict) else "-"
                    top_dims.append(f"{dim.label}={s}")
                dims_str = " | ".join(top_dims[:4])
            print(f"║  {icon} {label}：{e.total_score:.0f}分  [{e.latency_seconds:.0f}s]")
            if dims_str:
                print(f"║     {dims_str}")
            if e.issues:
                for issue in e.issues[:2]:
                    print(f"║     ⚠ {str(issue)[:W-8]}")
            if e.issues or dims_str:
                print("║" + " " * W + "║")

        # 跨 Agent 检查
        if self.cross_agent_check:
            cac = self.cross_agent_check
            icon = "✓" if cac.passed else "✗"
            print(f"║  {icon} 跨Agent一致性：{cac.score}分  [{len(cac.checks)}项检查]")
            for c in cac.checks:
                ci = "✓" if c["passed"] else "✗"
                # 截断详情
                detail = c.get("detail", "")[:W - 12]
                print(f"║     {ci} {c['name']}: {detail}")
            if cac.issues:
                for issue in cac.issues[:3]:
                    print(f"║     ⚠ {str(issue)[:W-8]}")

        # 汇总
        print("╠" + "═" * W + "╣")
        icon = "✓" if self.overall_passed else "✗"
        print(f"║  {icon} 综合评分：{self.overall_score:.0f}/100  "
              f"总耗时：{self.total_latency_seconds:.0f}s")
        print("╚" + "═" * W + "╝")
        print()


# ═══════════════════════════════════════════════════════════════
# NovelAgentHarness — 主 Harness
# ═══════════════════════════════════════════════════════════════

class NovelAgentHarness:
    """评测主控"""

    def __init__(self, scenarios: list[TestScenario] = None):
        self.scenarios = scenarios or BUILTIN_SCENARIOS
        self.reports: list[HarnessReport] = []

        # 评估器
        self.rough_evaluator = RoughOutlineEvaluator()
        self.detailed_evaluator = DetailedOutlineEvaluator()
        self.writing_evaluator = ChapterWritingEvaluator()
        self.maintenance_evaluator = SettingMaintenanceEvaluator()
        self.cross_checker = CrossAgentChecker()

    # ── 主入口 ────────────────────────────────────────────────

    def run_all(self, skip_api: bool = False) -> list[HarnessReport]:
        """运行所有场景的评测

        Args:
            skip_api: True=只做本地跨Agent检查，不调用LLM-as-Judge
        """
        print("╔" + "═" * 65 + "╗")
        print("║" + "  Novel Agent Harness — 评测启动".center(55) + "║")
        print("║" + f"  场景数：{len(self.scenarios)}".ljust(56) + "║")
        print("║" + f"  模式：{'仅本地检查' if skip_api else 'LLM-as-Judge + 本地检查'}".ljust(56) + "║")
        print("╚" + "═" * 65 + "╝")

        self.reports = []
        for i, scenario in enumerate(self.scenarios, 1):
            print(f"\n{'─' * 65}")
            print(f"  场景 {i}/{len(self.scenarios)}：{scenario.name}")
            print(f"{'─' * 65}")

            try:
                report = self._run_scenario(scenario, skip_api)
                self.reports.append(report)
                report.print_console()
            except Exception as e:
                print(f"  ✗ 场景执行失败: {e}")
                import traceback
                traceback.print_exc()

        self._print_summary()
        return self.reports

    def _run_scenario(self, scenario: TestScenario, skip_api: bool) -> HarnessReport:
        """运行单个场景"""
        total_start = time.time()

        # 1. 构建 ProjectConfig
        config = ProjectConfig(
            project_name=f"Harness_{scenario.id}",
            genre=scenario.genre,
            narrative_person=scenario.narrative_person,
            writing_style=scenario.writing_style,
            internet_slang_level=scenario.internet_slang_level,
            core_idea=scenario.core_idea,
            core_setting=scenario.core_setting,
            words_per_chapter=scenario.words_per_chapter,
            author_notes=scenario.author_notes,
        )

        # 2. 初始化设定库
        if scenario.preset_settings:
            setting_library = scenario.preset_settings
        else:
            print("  ⏳ 初始化设定库...")
            try:
                setting_library = self._init_settings(config)
            except Exception as e:
                print(f"  ✗ 设定库初始化失败（降级为空设定库继续）: {e}")
                setting_library = SettingLibrary()

        slm = SettingLibraryManager(setting_library, config)
        try:
            setting_summary = slm.get_summary(current_chapter=1)
        except Exception as e:
            print(f"  ✗ 设定库摘要生成失败（降级为空摘要）: {e}")
            setting_summary = ""

        # 3. 粗纲生成 + 评估
        rough_outline = None
        rough_eval = None
        if "rough_outline" in scenario.eval_agents:
            print("  ⏳ 生成粗纲...")
            try:
                rough_outline = self._generate_rough_outline(config, setting_library, setting_summary)
            except Exception as e:
                print(f"  ✗ 粗纲生成失败: {e}")
                rough_outline = None

            if not skip_api and rough_outline is not None:
                rough_eval = self.rough_evaluator.evaluate(
                    input_data={
                        "core_idea": scenario.core_idea,
                        "core_setting": scenario.core_setting,
                        "genre": scenario.genre,
                        "setting_summary": setting_summary,
                    },
                    output_data={
                        "outline_text": rough_outline.narrative_outline if rough_outline else "",
                        "raw": json.dumps(rough_outline.__dict__, ensure_ascii=False, default=str)
                        if rough_outline else "",
                    },
                )

        # 4. 细纲生成 + 评估
        chapter_outline = None
        detailed_eval = None
        if "detailed_outline" in scenario.eval_agents:
            print("  ⏳ 生成第1章细纲...")
            try:
                chapter_outline = self._generate_detailed_outline(
                    config, setting_library, rough_outline, setting_summary,
                )
            except Exception as e:
                print(f"  ✗ 细纲生成失败: {e}")
                chapter_outline = None

            if not skip_api and chapter_outline is not None:
                detailed_eval = self.detailed_evaluator.evaluate(
                    input_data={
                        "volume_outline_summary": rough_outline.narrative_outline[:2000]
                        if rough_outline else "",
                        "setting_summary": setting_summary,
                        "previous_summary": "（第一章）",
                    },
                    output_data={
                        "outline_json": json.dumps(chapter_outline.__dict__, ensure_ascii=False, default=str)
                        if chapter_outline else "",
                    },
                )

        # 5. 章节写作 + 评估
        chapter_content = None
        writing_eval = None
        if "chapter_writing" in scenario.eval_agents and chapter_outline:
            print("  ⏳ 生成第1章正文...")
            try:
                chapter_content = self._write_chapter(
                    config, setting_library, chapter_outline, setting_summary,
                )
            except Exception as e:
                print(f"  ✗ 正文生成失败: {e}")
                chapter_content = None

            if not skip_api and chapter_content is not None:
                writing_eval = self.writing_evaluator.evaluate(
                    input_data={
                        "outline_json": json.dumps(chapter_outline.__dict__, ensure_ascii=False, default=str)
                        if chapter_outline else "",
                        "setting_summary": setting_summary,
                        "writing_style": scenario.writing_style,
                        "internet_slang_level": scenario.internet_slang_level,
                        "narrative_person": scenario.narrative_person,
                        "words_per_chapter": scenario.words_per_chapter,
                    },
                    output_data={
                        "content": chapter_content.content if chapter_content else "",
                        "word_count": chapter_content.word_count if chapter_content else 0,
                    },
                )

        # 6. 设定库维护 + 评估
        try:
            settings_before = slm.get_summary(current_chapter=1)
        except Exception:
            settings_before = ""
        maintenance_eval = None
        updates_summary = {}
        if "setting_maintenance" in scenario.eval_agents and chapter_content and chapter_outline:
            print("  ⏳ 更新设定库...")
            try:
                updates_summary = self._run_maintenance(
                    config, setting_library, chapter_content, chapter_outline,
                )
            except Exception as e:
                print(f"  ✗ 设定库维护失败: {e}")
                updates_summary = {}

            settings_after = ""
            try:
                settings_after = slm.get_summary(current_chapter=1)
            except Exception:
                pass

            if not skip_api and updates_summary:
                maintenance_eval = self.maintenance_evaluator.evaluate(
                    input_data={
                        "chapter_content": chapter_content.content[:4000] if chapter_content else "",
                        "outline_json": json.dumps(chapter_outline.__dict__, ensure_ascii=False, default=str)
                        if chapter_outline else "",
                        "settings_before": settings_before,
                    },
                    output_data={
                        "settings_after": settings_after,
                        "new_entries": updates_summary.get("new_entries", 0),
                        "updates": updates_summary.get("updates", 0),
                    },
                )

        # 7. 跨 Agent 一致性检查
        cross_check = None
        if rough_outline and chapter_outline and chapter_content:
            try:
                cross_check = self.cross_checker.check_chain(
                    volume_outline=rough_outline,
                    chapter_outline=chapter_outline,
                    chapter_content=chapter_content,
                    setting_library=setting_library,
                    updates_summary=updates_summary,
                    target_words=scenario.words_per_chapter,
                )
            except Exception as e:
                print(f"  ✗ 跨Agent一致性检查失败: {e}")
                cross_check = None

        # 8. 计算综合评分
        scores = []
        if rough_eval: scores.append(rough_eval.total_score)
        if detailed_eval: scores.append(detailed_eval.total_score)
        if writing_eval: scores.append(writing_eval.total_score)
        if maintenance_eval: scores.append(maintenance_eval.total_score)
        if cross_check: scores.append(cross_check.score)

        overall = sum(scores) / len(scores) if scores else 0
        all_passed = all(
            e.passed for e in [rough_eval, detailed_eval, writing_eval, maintenance_eval]
            if e is not None
        )
        # 跨 Agent 一致性检查也参与通过判定（其分数已计入 overall）
        if cross_check is not None:
            all_passed = all_passed and cross_check.passed

        return HarnessReport(
            scenario_id=scenario.id,
            scenario_name=scenario.name,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            rough_outline_eval=rough_eval,
            detailed_outline_eval=detailed_eval,
            chapter_writing_eval=writing_eval,
            setting_maintenance_eval=maintenance_eval,
            cross_agent_check=cross_check,
            overall_score=round(overall, 1),
            overall_passed=all_passed,
            total_latency_seconds=round(time.time() - total_start, 1),
        )

    # ── 内部方法：调用真实 Agent ───────────────────────────────

    def _init_settings(self, config: ProjectConfig) -> SettingLibrary:
        """初始化设定库"""
        slm = SettingLibraryManager(SettingLibrary(), config)
        result = slm.generate_initial_settings()
        return slm.lib

    def _generate_rough_outline(
        self, config: ProjectConfig, setting_library: SettingLibrary,
        setting_summary: str,
    ) -> VolumeOutline:
        """生成粗纲"""
        agent = RoughOutlineAgent(config, setting_library)
        outline = agent.generate_volume_outline(
            volume_number=1,
            setting_summary=setting_summary,
            previous_summary="",
            unresolved_hooks="",
        )
        return outline

    def _generate_detailed_outline(
        self, config: ProjectConfig, setting_library: SettingLibrary,
        volume_outline: VolumeOutline, setting_summary: str,
    ) -> ChapterOutline:
        """生成细纲"""
        agent = DetailedOutlineAgent(config, setting_library)
        outline = agent.generate_chapter_outline(
            chapter_number=1,
            volume_outline=volume_outline,
            setting_summary=setting_summary,
            previous_chapters_summary="（这是第一章，无前文）",
            unresolved_hooks="（暂无）",
        )
        return outline

    def _write_chapter(
        self, config: ProjectConfig, setting_library: SettingLibrary,
        chapter_outline: ChapterOutline, setting_summary: str,
    ) -> ChapterContent:
        """写作章节"""
        agent = ChapterWritingAgent(config, setting_library)
        content = agent.write_chapter(
            chapter_outline=chapter_outline,
            setting_summary=setting_summary,
            previous_content_summary="（这是第一章，无前文）",
            previous_chapter_content="",
        )
        return content

    def _run_maintenance(
        self, config: ProjectConfig, setting_library: SettingLibrary,
        chapter_content: ChapterContent, chapter_outline: ChapterOutline,
    ) -> dict:
        """运行设定库维护"""
        agent = SettingMaintenanceAgent(config, setting_library)
        return agent.update_from_chapter(
            chapter_content=chapter_content,
            chapter_outline=chapter_outline,
            chapter_number=1,
        )

    # ── 汇总报告 ──────────────────────────────────────────────

    def _print_summary(self):
        """打印汇总报告"""
        if not self.reports:
            return

        W = 70
        print("\n\n" + "╔" + "═" * W + "╗")
        print("║" + "  评测汇总".center(W - 2) + "║")
        print("╠" + "═" * W + "╣")

        # 场景明细
        print(f"║ {'场景':<22} {'粗纲':>6} {'细纲':>6} {'写作':>6} {'维护':>6} {'一致性':>6} {'综合':>6} ║")
        print("╠" + "─" * W + "╣")

        for r in self.reports:
            name = r.scenario_name[:20]
            ro = f"{r.rough_outline_eval.total_score:.0f}" if r.rough_outline_eval else "-"
            do = f"{r.detailed_outline_eval.total_score:.0f}" if r.detailed_outline_eval else "-"
            cw = f"{r.chapter_writing_eval.total_score:.0f}" if r.chapter_writing_eval else "-"
            sm = f"{r.setting_maintenance_eval.total_score:.0f}" if r.setting_maintenance_eval else "-"
            ca = f"{r.cross_agent_check.score}" if r.cross_agent_check else "-"
            ov = f"{r.overall_score:.0f}"
            print(f"║ {name:<22} {ro:>6} {do:>6} {cw:>6} {sm:>6} {ca:>6} {ov:>6} ║")

        # 平均分
        print("╠" + "─" * W + "╣")
        avg_ro = self._avg([r.rough_outline_eval.total_score for r in self.reports if r.rough_outline_eval])
        avg_do = self._avg([r.detailed_outline_eval.total_score for r in self.reports if r.detailed_outline_eval])
        avg_cw = self._avg([r.chapter_writing_eval.total_score for r in self.reports if r.chapter_writing_eval])
        avg_sm = self._avg([r.setting_maintenance_eval.total_score for r in self.reports if r.setting_maintenance_eval])
        avg_ca = self._avg([r.cross_agent_check.score for r in self.reports if r.cross_agent_check])
        avg_ov = self._avg([r.overall_score for r in self.reports])

        print(f"║ {'平均':<22} {avg_ro:>6} {avg_do:>6} {avg_cw:>6} {avg_sm:>6} {avg_ca:>6} {avg_ov:>6} ║")

        # 通过率
        total_evals = sum(
            sum(1 for e in [r.rough_outline_eval, r.detailed_outline_eval,
                            r.chapter_writing_eval, r.setting_maintenance_eval]
                if e is not None)
            for r in self.reports
        )
        passed_evals = sum(
            sum(1 for e in [r.rough_outline_eval, r.detailed_outline_eval,
                            r.chapter_writing_eval, r.setting_maintenance_eval]
                if e is not None and e.passed)
            for r in self.reports
        )
        print("╠" + "═" * W + "╣")
        print(f"║  总通过率：{passed_evals}/{total_evals}（{passed_evals/total_evals*100:.0f}%）"
              if total_evals > 0 else "║  无评估数据")
        print("╚" + "═" * W + "╝")
        print()

    @staticmethod
    def _avg(values: list) -> str:
        if not values:
            return "-"
        return f"{sum(values)/len(values):.0f}"

    # ── 报告导出 ──────────────────────────────────────────────

    def export_reports(self, output_dir: str = None):
        """导出所有报告为 JSON"""
        if output_dir is None:
            output_dir = os.path.join(os.path.dirname(__file__), "data", "harness_reports")

        os.makedirs(output_dir, exist_ok=True)

        for report in self.reports:
            filename = f"{report.scenario_id}_{report.timestamp.replace(':', '-').replace(' ', '_')}.json"
            filepath = os.path.join(output_dir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
            print(f"  报告已导出：{filepath}")

        # 汇总
        summary_path = os.path.join(output_dir, "summary.json")
        summary = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_scenarios": len(self.reports),
            "reports": [r.to_dict() for r in self.reports],
        }
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"  汇总已导出：{summary_path}")


# ═══════════════════════════════════════════════════════════════
# Dry-run 模式：仅本地逻辑检查，不调 API
# ═══════════════════════════════════════════════════════════════

class HarnessDryRun:
    """不调 API 的轻量检查：仅验证数据流转和接口衔接"""

    def run(self) -> dict:
        """运行 dry-run 检查"""
        print("╔" + "═" * 65 + "╗")
        print("║" + "  Harness Dry-Run — 本地逻辑检查".center(55) + "║")
        print("╚" + "═" * 65 + "╝")

        results = {
            "imports": self._check_imports(),
            "evaluators": self._check_evaluators(),
            "cross_checker": self._check_cross_checker(),
            "report": self._check_report(),
            "scenarios": self._check_scenarios(),
        }

        all_ok = all(r["passed"] for r in results.values())
        print(f"\n  {'✓ Dry-run 通过' if all_ok else '✗ Dry-run 失败'}")
        return results

    def _check_imports(self) -> dict:
        checks = []
        try:
            from harness import NovelAgentHarness, HarnessReport, CrossAgentChecker
            checks.append("✓")
        except Exception as e:
            checks.append(f"✗ {e}")
        try:
            from harness import RoughOutlineEvaluator, DetailedOutlineEvaluator
            checks.append("✓")
        except Exception as e:
            checks.append(f"✗ {e}")
        try:
            from harness import ChapterWritingEvaluator, SettingMaintenanceEvaluator
            checks.append("✓")
        except Exception as e:
            checks.append(f"✗ {e}")
        try:
            from harness import BUILTIN_SCENARIOS, TestScenario
            checks.append("✓")
        except Exception as e:
            checks.append(f"✗ {e}")
        return {"passed": all("✓" in c for c in checks), "checks": checks}

    def _check_evaluators(self) -> dict:
        checks = []
        for name, cls, dims in [
            ("RoughOutlineEvaluator", RoughOutlineEvaluator, ROUGH_OUTLINE_DIMS),
            ("DetailedOutlineEvaluator", DetailedOutlineEvaluator, DETAILED_OUTLINE_DIMS),
            ("ChapterWritingEvaluator", ChapterWritingEvaluator, CHAPTER_WRITING_DIMS),
            ("SettingMaintenanceEvaluator", SettingMaintenanceEvaluator, SETTING_MAINTENANCE_DIMS),
        ]:
            try:
                e = cls()
                assert len(e.dimensions) == len(dims)
                assert abs(sum(d.weight for d in e.dimensions) - 1.0) < 0.01
                checks.append(f"✓ {name}（{len(dims)}维，权重和=1.0）")
            except Exception as ex:
                checks.append(f"✗ {name}: {ex}")
        return {"passed": all("✓" in c for c in checks), "checks": checks}

    def _check_cross_checker(self) -> dict:
        checks = []
        try:
            cc = CrossAgentChecker()
            # 构建最小测试数据
            vol = VolumeOutline(
                volume_number=1, narrative_outline="测试粗纲",
                foreshadowing_planted=["伏笔A"],
            )
            ch = ChapterOutline(
                chapter_number=1, chapter_title="测试",
                chapter_objective="测试目标",
                characters_appearing=["主角"], locations=["测试地点"],
                foreshadowing_plant=["伏笔A"],
                hooks_set=["钩子1"],
            )
            content = ChapterContent(
                chapter_number=1, title="测试",
                content="主角在测试地点做了一些事情。钩子1似乎有异动。",
                word_count=150,
            )
            settings = SettingLibrary(
                characters={"主角": CharacterEntry(name="主角", importance="core")},
            )
            updates = {"new_entries": 1, "updates": 0}

            result = cc.check_chain(vol, ch, content, settings, updates)
            assert result is not None
            assert len(result.checks) == 7
            checks.append(f"✓ CrossAgentChecker（{len(result.checks)}项检查）")
        except Exception as e:
            checks.append(f"✗ {e}")
        return {"passed": all("✓" in c for c in checks), "checks": checks}

    def _check_report(self) -> dict:
        checks = []
        try:
            report = HarnessReport(
                scenario_id="test", scenario_name="测试",
                timestamp="2026-01-01",
                overall_score=85.0, overall_passed=True,
            )
            d = report.to_dict()
            assert d["scenario_id"] == "test"
            assert d["overall_score"] == 85.0
            checks.append("✓ HarnessReport.to_dict()")
        except Exception as e:
            checks.append(f"✗ {e}")
        try:
            report = HarnessReport(
                scenario_id="test", scenario_name="测试",
                timestamp="2026-01-01",
            )
            report.print_console()
            checks.append("✓ HarnessReport.print_console()")
        except Exception as e:
            checks.append(f"✗ {e}")
        return {"passed": all("✓" in c for c in checks), "checks": checks}

    def _check_scenarios(self) -> dict:
        checks = []
        try:
            assert len(BUILTIN_SCENARIOS) == 3
            for s in BUILTIN_SCENARIOS:
                assert s.id
                assert s.name
                assert s.core_idea
                assert s.genre
            checks.append(f"✓ 内置场景 {len(BUILTIN_SCENARIOS)} 个")
        except Exception as e:
            checks.append(f"✗ {e}")
        return {"passed": all("✓" in c for c in checks), "checks": checks}


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Novel Agent 评测 Harness")
    parser.add_argument("--dry-run", action="store_true", help="只做本地逻辑检查，不调 API")
    parser.add_argument("--scenario", type=str, help="指定场景 ID，逗号分隔多个")
    parser.add_argument("--skip-api-eval", action="store_true",
                        help="跳过 LLM-as-Judge 评估，只做 Agent 生成 + 本地检查")
    parser.add_argument("--export", action="store_true", help="导出报告 JSON")
    parser.add_argument("--output-dir", type=str, help="报告导出目录")

    args = parser.parse_args()

    if args.dry_run:
        HarnessDryRun().run()
        sys.exit(0)

    # 选择场景
    if args.scenario:
        ids = set(args.scenario.split(","))
        scenarios = [s for s in BUILTIN_SCENARIOS if s.id in ids]
        if not scenarios:
            print(f"未找到场景: {args.scenario}")
            print(f"可用场景: {[s.id for s in BUILTIN_SCENARIOS]}")
            sys.exit(1)
    else:
        scenarios = BUILTIN_SCENARIOS

    # 运行
    harness = NovelAgentHarness(scenarios)
    reports = harness.run_all(skip_api=args.skip_api_eval)

    if args.export:
        harness.export_reports(args.output_dir)