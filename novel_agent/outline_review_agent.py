"""
小纲审查 Agent — 检查细纲与设定库的一致性
"""

import json
from models import ChapterOutline, ProjectConfig, SettingLibrary, ReviewResult
from api_client import get_client
from prompts import OUTLINE_REVIEW_SYSTEM, get_outline_review_user
from utils import print_subheader, print_success, print_warning, print_section


class OutlineReviewAgent:
    """小纲审查 Agent"""

    def __init__(self, config: ProjectConfig, setting_library: SettingLibrary):
        self.config = config
        self.setting_library = setting_library

    def review(
        self,
        chapter_outline: ChapterOutline,
        setting_summary: str,
        unresolved_hooks: str = "",
    ) -> ReviewResult:
        """
        审查章节细纲是否符合设定库要求。

        Returns:
            ReviewResult: 审查结果，包含 passed、score、issues、suggestions、detail
        """
        print_subheader(f"审查第 {chapter_outline.chapter_number} 章细纲")
        print("  正在调用 AI 审查细纲与设定库的一致性...")

        outline_json = json.dumps(chapter_outline.__dict__, ensure_ascii=False, indent=2, default=str)

        client = get_client()
        user_prompt = get_outline_review_user(
            chapter_number=chapter_outline.chapter_number,
            chapter_outline_json=outline_json,
            setting_summary=setting_summary,
            unresolved_hooks=unresolved_hooks,
        )

        result = {}
        for attempt in range(2):
            result = client.chat_with_json_output(
                OUTLINE_REVIEW_SYSTEM, user_prompt,
                temperature=0.3, max_tokens=8192,
            )
            if "_parse_error" not in result:
                break
            if attempt == 0:
                print_warning("审查结果 JSON 解析失败，重试一次...")

        if "_parse_error" in result:
            # 解析失败不允许静默放行：返回不通过，由调用方标记 needs_attention
            print_warning("审查结果解析失败（已重试），本细纲标记为需人工关注")
            return ReviewResult(
                passed=False,
                score=0,
                issues=[{"severity": "error", "category": "parse_error",
                         "description": "AI 审查结果解析失败"}],
                suggestions=["请人工审核本细纲"],
                detail=result.get("_raw", ""),
            )

        try:
            score = int(result.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        # passed 由代码按分数判定，不采信模型自报的 passed 字段
        review = ReviewResult(
            passed=score >= 70,
            score=score,
            issues=result.get("issues", []),
            suggestions=result.get("suggestions", []),
            detail=result.get("detail", ""),
        )

        self._display_review(review, chapter_outline.chapter_number)
        return review

    def _display_review(self, review: ReviewResult, chapter_number: int):
        """展示审查结果"""
        status = "✓ 通过" if review.passed else "✗ 不通过"
        print(f"\n  审查结果：{status}（评分：{review.score}/100）")

        if review.issues:
            print_section(f"发现 {len(review.issues)} 个问题")
            for issue in review.issues:
                if isinstance(issue, dict):
                    sev = issue.get("severity", "warning")
                    cat = issue.get("category", "")
                    desc = issue.get("description", "")
                    icon = "❌" if sev == "error" else "⚠️"
                    print(f"    {icon} [{cat}] {desc}")
                else:
                    print(f"    · {issue}")

        if review.suggestions:
            print_section("修改建议")
            for s in review.suggestions:
                print(f"    · {s}")

        if review.detail:
            print_section("详细报告")
            print(f"    {review.detail[:500]}")
