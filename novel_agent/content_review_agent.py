"""
文章校验 Agent — 检查章节正文与细纲的匹配度
"""

import json
from models import ChapterOutline, ChapterContent, ProjectConfig, SettingLibrary, ReviewResult
from api_client import get_client
from prompts import CONTENT_REVIEW_SYSTEM, get_content_review_user
from utils import print_subheader, print_success, print_warning, print_section


class ContentReviewAgent:
    """文章校验 Agent"""

    def __init__(self, config: ProjectConfig, setting_library: SettingLibrary):
        self.config = config
        self.setting_library = setting_library

    def review(
        self,
        chapter_content: ChapterContent,
        chapter_outline: ChapterOutline,
        setting_summary: str,
    ) -> ReviewResult:
        """
        校验章节正文是否与细纲一致。

        Returns:
            ReviewResult: 校验结果
        """
        print_subheader(f"校验第 {chapter_outline.chapter_number} 章正文")

        # 提取正文内容（去除检查清单）
        content_text = chapter_content.content
        checklist_marker = "---写作检查---"
        if checklist_marker in content_text:
            content_text = content_text.split(checklist_marker)[0].strip()

        print("  正在调用 AI 校验正文与细纲的匹配度...")

        outline_json = json.dumps(chapter_outline.__dict__, ensure_ascii=False, indent=2, default=str)

        client = get_client()
        user_prompt = get_content_review_user(
            chapter_number=chapter_outline.chapter_number,
            chapter_content=content_text,
            chapter_outline_json=outline_json,
            setting_summary=setting_summary,
            target_word_count=self.config.words_per_chapter,
        )

        result = {}
        for attempt in range(2):
            result = client.chat_with_json_output(
                CONTENT_REVIEW_SYSTEM, user_prompt,
                temperature=0.3, max_tokens=8192,
            )
            if "_parse_error" not in result:
                break
            if attempt == 0:
                print_warning("校验结果 JSON 解析失败，重试一次...")

        if "_parse_error" in result:
            # 解析失败不允许静默放行：返回不通过，由调用方标记 needs_attention
            print_warning("校验结果解析失败（已重试），本章标记为需人工关注")
            return ReviewResult(
                passed=False,
                score=0,
                issues=[{"severity": "error", "category": "parse_error",
                         "description": "AI 校验结果解析失败"}],
                suggestions=["请人工审核本章正文"],
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

        self._display_review(review, chapter_outline.chapter_number, chapter_content)
        return review

    def _display_review(self, review: ReviewResult, chapter_number: int, content: ChapterContent):
        """展示校验结果"""
        status = "✓ 通过" if review.passed else "✗ 不通过"
        print(f"\n  校验结果：{status}（评分：{review.score}/100）")

        # 字数检查
        target = self.config.words_per_chapter
        actual = content.word_count
        diff_pct = abs(actual - target) / target * 100
        print(f"  字数：{actual} / {target}（偏差 {diff_pct:.1f}%）")

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
