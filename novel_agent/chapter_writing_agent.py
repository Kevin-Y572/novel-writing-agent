"""
章节写作 Agent — 根据细纲生成章节正文
"""

import json
from datetime import datetime
from models import ChapterOutline, ChapterContent, ProjectConfig, SettingLibrary
from api_client import get_client
from prompts import CHAPTER_WRITING_SYSTEM, get_chapter_writing_user
from utils import print_header, print_subheader, print_success, print_warning, ask, ask_yes_no


class ChapterWritingAgent:
    """章节写作 Agent"""

    def __init__(self, config: ProjectConfig, setting_library: SettingLibrary):
        self.config = config
        self.setting_library = setting_library

    def write_chapter(
        self,
        chapter_outline: ChapterOutline,
        setting_summary: str,
        previous_content_summary: str = "",
        previous_chapter_content: str = "",
    ) -> ChapterContent | None:
        """
        根据细纲生成章节正文。

        Returns:
            ChapterContent: 章节正文对象，失败返回 None
        """
        print_header(f"撰写第 {chapter_outline.chapter_number} 章正文")
        print(f"  目标字数：{self.config.words_per_chapter} 字")
        print(f"  文风：{self.config.writing_style}")
        print("  正在调用 AI 生成章节正文...（可能需要 60-120 秒）")

        outline_json = json.dumps(chapter_outline.__dict__, ensure_ascii=False, indent=2, default=str)

        scene_budgets = self.build_scene_budgets(chapter_outline.scenes,
                                                 self.config.words_per_chapter)

        # 构建前文内容摘要（包含上一章末尾内容作为衔接）
        content_context = previous_content_summary
        if previous_chapter_content:
            # 取上一章最后 500 字作为衔接参考
            last_part = previous_chapter_content[-500:] if len(previous_chapter_content) > 500 else previous_chapter_content
            content_context += f"\n\n【上一章末尾内容（供衔接参考）】\n{last_part}"

        client = get_client()
        user_prompt = get_chapter_writing_user(
            chapter_number=chapter_outline.chapter_number,
            chapter_outline_json=outline_json,
            setting_summary=setting_summary,
            previous_content_summary=content_context,
            writing_style=self.config.writing_style,
            internet_slang_level=self.config.internet_slang_level,
            narrative_person=self.config.narrative_person,
            genre=self.config.genre,
            words_per_chapter=self.config.words_per_chapter,
            scene_budgets=scene_budgets,
        )

        try:
            raw = ""
            # flash 偶发返回空内容，自动重试（最多 3 次尝试）
            for attempt in range(3):
                raw = client.chat(
                    CHAPTER_WRITING_SYSTEM, user_prompt,
                    temperature=0.8, max_tokens=8192,
                )
                if raw:
                    break
                print_warning(f"AI 返回空内容，重试（第 {attempt + 1}/3 次）...")
        except Exception as e:
            print_warning(f"章节写作 API 调用失败: {e}")
            return None

        if not raw:
            print_warning("AI 连续返回空内容，放弃生成")
            return None

        # 统计字数（去除写作检查清单部分）
        content = raw
        checklist_marker = "---写作检查---"
        if checklist_marker in content:
            content = content.split(checklist_marker)[0].strip()

        word_count = len(content)

        chapter_content = ChapterContent(
            chapter_number=chapter_outline.chapter_number,
            title=chapter_outline.chapter_title,
            content=raw,  # 保留完整内容（含检查清单）
            word_count=word_count,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            revision_count=0,
        )

        self._display_content(chapter_content)
        return chapter_content

    def revise_chapter(
        self,
        current_content: ChapterContent,
        feedback: str,
        chapter_outline: ChapterOutline,
        setting_summary: str,
    ) -> ChapterContent | None:
        """
        根据反馈修改章节正文。

        Returns:
            ChapterContent: 修改后的章节正文，失败返回 None
        """
        print("\n  正在根据反馈修改章节内容...")

        client = get_client()
        revision_prompt = f"""请根据以下反馈修改第 {chapter_outline.chapter_number} 章的正文。

【当前正文】
{current_content.content[:12000]}

【章节细纲（任务书）】
{json.dumps(chapter_outline.__dict__, ensure_ascii=False, indent=2, default=str)}

【设定库概要】
{setting_summary}

【修改意见】
{feedback}

请输出修改后的完整正文（直接输出，不需要 JSON 包裹）。"""

        raw = ""
        try:
            # flash 偶发返回空内容，与 write_chapter 相同的重试策略
            for attempt in range(3):
                raw = client.chat(
                    CHAPTER_WRITING_SYSTEM, revision_prompt,
                    temperature=0.7, max_tokens=8192,
                )
                if raw:
                    break
                print_warning(f"AI 返回空内容，重试（第 {attempt + 1}/3 次）...")
        except Exception as e:
            print_warning(f"修改章节 API 调用失败: {e}")
            return None

        if not raw:
            return None

        content = raw
        checklist_marker = "---写作检查---"
        if checklist_marker in content:
            content = content.split(checklist_marker)[0].strip()

        word_count = len(content)

        revised = ChapterContent(
            chapter_number=current_content.chapter_number,
            title=current_content.title,
            content=raw,
            word_count=word_count,
            created_at=current_content.created_at,
            revision_count=current_content.revision_count + 1,
        )

        print_success("修改完成！")
        self._display_content(revised)
        return revised

    # ── 字数硬控（程序化扩写/收缩） ─────────────────────────

    BUDGET_SCALE = 0.97  # 写手执行预算平均超支 ~3%，按比例下调瞄准点

    @staticmethod
    def build_scene_budgets(scenes: list, target_words: int,
                            scale: float = None) -> str:
        """把细纲场景预算渲染为分段硬预算文本。
        预算总和无条件缩放到 target×scale（实测写手执行预算平均超支，
        直接给足额预算必然首发越界——瞄准点补偿在预算层的应用）。
        无预算数据的场景标注"精简篇幅"。"""
        scale = ChapterWritingAgent.BUDGET_SCALE if scale is None else scale
        aim_total = int(target_words * scale)
        ests = []
        for scene in (scenes or []):
            if not isinstance(scene, dict):
                ests.append(None)
                continue
            try:
                ests.append(int(scene.get("estimated_words") or 0) or None)
            except (TypeError, ValueError):
                ests.append(None)
        known = [e for e in ests if e]
        if not known:
            return ""

        factor = aim_total / sum(known)
        lines, running = [], 0
        for i, est in enumerate(ests, 1):
            loc = (scenes[i - 1] or {}).get("location", f"场景{i}")
            if est:
                scaled = max(200, int(round(est * factor / 10.0)) * 10)  # 取整到10字
                running += scaled
                lines.append(f"- 场景{i}（{loc}）：{scaled} 字（硬上限 {int(scaled * 1.1)} 字，写到即收束）")
            else:
                lines.append(f"- 场景{i}（{loc}）：精简篇幅，点到即止")
        return ("- 逐场景字数预算（硬约束：单场景超出上限即收束；合计瞄准 "
                f"{aim_total} 字，写手超支后落点仍在总区间内）：\n  "
                + "\n  ".join(lines) + f"\n  合计：{running} 字\n")

    @staticmethod
    def _strip_checklist(raw: str) -> str:
        marker = "---写作检查---"
        return raw.split(marker)[0].strip() if marker in raw else (raw or "").strip()

    @staticmethod
    def _aim(count: int, lo: int, hi: int, target: int) -> int:
        """指令瞄准点补偿：模型对压缩/扩写幅度的执行总是打折（让它删 400 字它删 200）。
        指令瞄准越过目标的位置，实际落点才会进入区间。"""
        if count > hi:
            return max(lo, target - (count - target) // 2)
        if count < lo:
            return min(hi, target + (target - count) // 2)
        return target

    def enforce_word_count(
        self,
        chapter_content: ChapterContent,
        chapter_outline: ChapterOutline,
        setting_summary: str,
        tolerance: float = 0.10,
    ) -> ChapterContent:
        """字数落在目标 ±tolerance 之外时定向扩写/收缩，最多三轮交替收敛。
        每轮后复查；仍越界则保留历史版本中最接近目标的一版。区间内直接返回（零 API）。"""
        target = self.config.words_per_chapter
        lo, hi = int(target * (1 - tolerance)), int(target * (1 + tolerance))
        count = len(self._strip_checklist(chapter_content.content))
        if lo <= count <= hi:
            return chapter_content

        best_content, best_dist = chapter_content, abs(count - target)
        result = chapter_content
        prev_count = count
        for _ in range(3):  # 扩写↔收缩交替收敛；无进展提前停
            result = self._one_word_count_pass(result, chapter_outline,
                                               setting_summary, lo, hi, target)
            if result is best_content:  # 调用失败保留原稿，退出避免空转
                break
            new_count = len(self._strip_checklist(result.content))
            dist = abs(new_count - target)
            if dist < best_dist:
                best_content, best_dist = result, dist
            if lo <= new_count <= hi:
                break
            if abs(new_count - target) >= abs(prev_count - target):  # 无进展
                break
            prev_count = new_count
        return best_content

    def _one_word_count_pass(self, chapter_content: ChapterContent,
                             chapter_outline: ChapterOutline, setting_summary: str,
                             lo: int, hi: int, target: int) -> ChapterContent:
        """单轮定向扩写/收缩（返回原稿表示本轮失败）。指令瞄准 _aim 补偿点。"""
        current = self._strip_checklist(chapter_content.content)
        count = len(current)
        if lo <= count <= hi:
            return chapter_content
        aim = self._aim(count, lo, hi, target)

        if count < lo:
            direction = (f"当前正文只有 {count} 字，低于下限 {lo} 字。请在保持已有情节与结尾钩子不变的前提下扩写："
                         f"深化关键动作与对抗的细节、补充人物对话交锋、把一笔带过的转折写实，"
                         f"扩写到约 {aim} 字。禁止注水式重复和无关支线。")
        else:
            direction = (f"当前正文 {count} 字，超过上限 {hi} 字。请压缩到约 {aim} 字："
                         f"合并冗余对话、删减重复渲染与信息密度过高的赶场段落，"
                         f"但关键情绪点的驻留描写和场景衔接必须保留，"
                         f"核心任务、伏笔与结尾钩子一个都不能丢。")

        print(f"  ⚙ 字数越界（{count} 字，目标 {lo}-{hi}，瞄准 {aim}），触发定向{'扩写' if count < lo else '收缩'}...")
        prompt = f"""请调整第 {chapter_outline.chapter_number} 章正文的篇幅。

【当前正文】
{current}

【章节细纲（任务书）】
{json.dumps(chapter_outline.__dict__, ensure_ascii=False, indent=2, default=str)}

【设定库概要】
{setting_summary}

【篇幅调整要求】
{direction}

请输出调整后的完整正文（直接输出，不需要 JSON 包裹，也不要附写作检查清单）。"""

        try:
            client = get_client()
            raw = client.chat(CHAPTER_WRITING_SYSTEM, prompt,
                              temperature=0.6, max_tokens=8192)
        except Exception as e:
            print_warning(f"字数调整 API 调用失败，保留原稿: {e}")
            return chapter_content

        if not raw:
            print_warning("字数调整返回空内容，保留原稿")
            return chapter_content

        new_count = len(self._strip_checklist(raw))
        # 调整后必须仍然可读且更接近目标，否则保留原稿
        if new_count < max(lo - 300, 500):
            print_warning(f"字数调整结果异常（{new_count} 字），保留原稿")
            return chapter_content

        revised = ChapterContent(
            chapter_number=chapter_content.chapter_number,
            title=chapter_content.title,
            content=raw,
            word_count=new_count,
            created_at=chapter_content.created_at,
            revision_count=chapter_content.revision_count + 1,
        )
        print(f"  ✓ 字数调整完成：{count} → {new_count} 字")
        return revised

    def interactive_writing_loop(
        self,
        chapter_outline: ChapterOutline,
        setting_summary: str,
        previous_content_summary: str = "",
        previous_chapter_content: str = "",
    ) -> ChapterContent | None:
        """
        交互式章节写作循环：
        生成 → 展示 → 用户反馈 → 修改 → 确认
        """
        content = self.write_chapter(
            chapter_outline, setting_summary,
            previous_content_summary, previous_chapter_content,
        )

        if content is None:
            if not ask_yes_no("生成失败，是否重试？", default="y"):
                return None
            content = self.write_chapter(
                chapter_outline, setting_summary,
                previous_content_summary, previous_chapter_content,
            )
            if content is None:
                print_warning("生成再次失败")
                return None

        while True:
            print()
            action = input("  [确认(y) / 修改意见 / 重新生成(r)]: ").strip()

            if action.lower() in ("y", "yes", "确认", ""):
                print_success(f"第 {chapter_outline.chapter_number} 章正文已确认！")
                return content

            elif action.lower() in ("r", "重新生成", "重试"):
                content = self.write_chapter(
                    chapter_outline, setting_summary,
                    previous_content_summary, previous_chapter_content,
                )
                if content is None:
                    continue

            elif action.strip():
                content = self.revise_chapter(
                    content, action, chapter_outline, setting_summary,
                )
                if content is None:
                    continue

    def _display_content(self, content: ChapterContent):
        """展示章节正文摘要"""
        print_subheader(f"第 {content.chapter_number} 章：{content.title}")
        print(f"  字数：{content.word_count} 字")
        print(f"  修改次数：{content.revision_count}")

        # 展示正文前 500 字作为预览
        preview = content.content[:500]
        print(f"\n  ── 正文预览（前500字）──")
        for line in preview.split("\n"):
            if line.strip():
                print(f"  {line.strip()}")
        if len(content.content) > 500:
            print(f"  ...（共 {content.word_count} 字，完整内容已保存）")
