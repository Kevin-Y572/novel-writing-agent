"""
细纲 Agent — 任务式章节规划
"""

import json
import textwrap
from models import ChapterOutline, VolumeOutline, ProjectConfig, SettingLibrary
from api_client import get_client
from setting_library import is_dead, RECALL_CONTEXT_KEYWORDS
from prompts import DETAILED_OUTLINE_SYSTEM, get_detailed_outline_user
from utils import (
    print_header, print_subheader, print_section,
    print_success, print_warning, print_list,
    ask, ask_yes_no,
)


class DetailedOutlineAgent:
    """细纲生成 Agent"""

    def __init__(self, config: ProjectConfig, setting_library: SettingLibrary):
        self.config = config
        self.setting_library = setting_library

    def generate_chapter_outline(
        self,
        chapter_number: int,
        volume_outline: VolumeOutline,
        setting_summary: str,
        previous_chapters_summary: str = "",
        unresolved_hooks: str = "",
        revision_feedback: str = "",
        pacing_constraint: str = "",
    ) -> ChapterOutline:
        """生成单章细纲。revision_feedback 非空时为审查未通过后的重新生成"""
        print_header(f"第 {chapter_number} 章细纲生成" + ("（修正版）" if revision_feedback else ""))

        # 从粗纲中提取本章相关段落
        relevant_section = self._extract_relevant_section(
            volume_outline, chapter_number
        )

        print("  正在调用 AI 生成任务式细纲...（可能需要 20-40 秒）")

        client = get_client()
        user_prompt = get_detailed_outline_user(
            chapter_number=chapter_number,
            volume_outline_section=relevant_section,
            volume_title=volume_outline.volume_title,
            setting_summary=setting_summary,
            previous_chapters_summary=previous_chapters_summary,
            unresolved_hooks=unresolved_hooks,
            words_per_chapter=self.config.words_per_chapter,
            revision_feedback=revision_feedback,
            pacing_constraint=pacing_constraint,
        )

        result = client.chat_with_json_output(
            DETAILED_OUTLINE_SYSTEM, user_prompt,
            temperature=0.7, max_tokens=16384,
        )

        if "_parse_error" in result:
            print_warning("AI 返回的 JSON 无法解析")
            print(f"\n{result.get('_raw', '')[:2000]}...")
            return None

        outline = self._dict_to_outline(result, chapter_number)
        # 规范化出场人物/地点名（模型偶发输出"林上（主角）"式装饰名，导致后续匹配失效）
        from consistency_checker import normalize_outline_names
        changed = normalize_outline_names(outline)
        if changed:
            print(f"  已规范化 {changed} 个带装饰的人物/地点名称")
        trimmed = self.enforce_task_cap(outline)
        if trimmed:
            print(f"  ⚙ 任务清单超上限，确定性裁剪 {trimmed} 项（保留伏笔/钩子优先）")
        self._display_outline(outline)

        # 写前守卫：已死亡/退场角色被安排出场 → 警告（不阻断，交互模式下由用户决定）
        dead_violations = self.check_dead_characters(outline)
        if dead_violations:
            print_warning("⚠ 检测到情节状态矛盾（写前守卫，建议修改细纲）：")
            for v in dead_violations:
                print(f"    · {v}")

        return outline

    # ── 任务上限（防正文流水账化）────────────────────────────

    TASK_CAP = 8
    # 裁剪优先级（低→高）：世界观释放 最先裁，伏笔两维永远不裁（台账依赖）
    _TRIM_ORDER = ["world_building_revealed", "conflicts_advanced",
                   "character_updates", "hooks_set"]

    def enforce_task_cap(self, outline: ChapterOutline) -> int:
        """六维任务合计超 TASK_CAP 时确定性裁剪，返回裁掉的数量。"""
        dims = self._TRIM_ORDER + ["foreshadowing_plant", "foreshadowing_recover"]
        total = sum(len(getattr(outline, d) or []) for d in dims)
        removed = 0
        for dim in self._TRIM_ORDER:
            while total > self.TASK_CAP and getattr(outline, dim):
                getattr(outline, dim).pop()
                total -= 1
                removed += 1
            if total <= self.TASK_CAP:
                break
        return removed

    def check_dead_characters(self, outline: ChapterOutline) -> list:
        """写前守卫：细纲是否安排已死亡/退场角色出场（回忆/闪回/梦境/复活场景除外）"""
        if not outline or not outline.characters_appearing:
            return []
        scenes = [s for s in (outline.scenes or []) if isinstance(s, dict)]
        fallback_text = (outline.chapter_objective or "") + (outline.volume_reference or "")
        violations = []
        for name in outline.characters_appearing:
            if not name:
                continue
            entry = self.setting_library.characters.get(name)
            if entry is None:
                # 宽松匹配：细纲人名可能带修饰
                for n, e in self.setting_library.characters.items():
                    if name in n or n in name:
                        entry = e
                        break
            if not entry or not is_dead(getattr(entry, "current_status", "") or ""):
                continue
            # 仅检查该角色出现的场景是否为回忆/闪回语境
            hit_scenes = [s for s in scenes if name in json.dumps(s, ensure_ascii=False)]
            scene_text = " ".join(json.dumps(s, ensure_ascii=False) for s in hit_scenes) if hit_scenes else fallback_text
            if not any(k in scene_text for k in RECALL_CONTEXT_KEYWORDS):
                status = (getattr(entry, "current_status", "") or "")[:40]
                violations.append(f"已死亡/退场角色「{name}」（{status}）被安排出场，且非回忆/闪回/复活剧情")
        return violations

    def interactive_outline_loop(
        self,
        chapter_number: int,
        volume_outline: VolumeOutline,
        setting_summary: str,
        previous_chapters_summary: str = "",
        unresolved_hooks: str = "",
    ) -> ChapterOutline:
        """
        交互式细纲生成循环：
        生成 → 展示 → 用户反馈 → 修改 → 确认
        """
        outline = self.generate_chapter_outline(
            chapter_number, volume_outline, setting_summary,
            previous_chapters_summary, unresolved_hooks,
        )

        if outline is None:
            if not ask_yes_no("生成失败，是否重试？", default="y"):
                return None
            outline = self.generate_chapter_outline(
                chapter_number, volume_outline, setting_summary,
                previous_chapters_summary, unresolved_hooks,
            )
            if outline is None:
                print_warning("生成再次失败")
                return None

        while True:
            print()
            action = input("  [确认(y) / 修改意见 / 重新生成(r) / 手动编辑(e)]: ").strip()

            if action.lower() in ("y", "yes", "确认", ""):
                print_success(f"第 {chapter_number} 章细纲已确认！")
                return outline

            elif action.lower() in ("r", "重新生成", "重试"):
                outline = self.generate_chapter_outline(
                    chapter_number, volume_outline, setting_summary,
                    previous_chapters_summary, unresolved_hooks,
                )
                if outline is None:
                    continue

            elif action.lower() in ("e", "编辑", "手动"):
                outline = self._manual_edit(outline)
                self._display_outline(outline)

            elif action.strip():
                # 视为修改意见
                outline = self._revise_outline(
                    outline, action, volume_outline, setting_summary,
                    previous_chapters_summary, unresolved_hooks,
                )
                if outline is None:
                    continue

    # ── 修改 ───────────────────────────────────────────────

    def _revise_outline(
        self,
        current: ChapterOutline,
        feedback: str,
        volume_outline: VolumeOutline,
        setting_summary: str,
        previous_summary: str = "",
        unresolved_hooks: str = "",
    ) -> ChapterOutline:
        """根据反馈修改细纲"""
        print("\n  正在根据你的反馈修改细纲...")

        client = get_client()
        revision_prompt = f"""请根据作者反馈修改第 {current.chapter_number} 章的细纲。

【当前细纲】
{json.dumps(current.__dict__, ensure_ascii=False, indent=2, default=str)}

【作者修改意见】
{feedback}

请输出修改后的完整细纲 JSON，格式与原来相同。直接输出 JSON。"""

        result = client.chat_with_json_output(
            DETAILED_OUTLINE_SYSTEM, revision_prompt,
            temperature=0.6, max_tokens=8192,
        )

        if "_parse_error" in result:
            print_warning("修改失败")
            return current

        outline = self._dict_to_outline(result, current.chapter_number)
        print_success("修改完成！")
        self._display_outline(outline)
        return outline

    def _manual_edit(self, current: ChapterOutline) -> ChapterOutline:
        """手动编辑：导出当前细纲为 JSON 文件，用系统默认编辑器打开"""
        import os
        import sys
        import tempfile

        print("\n  [手动编辑模式] 即将在编辑器中打开当前细纲的 JSON 文件")
        print("  修改后保存并关闭编辑器即可")

        # 写入临时文件
        tmp_path = os.path.join(tempfile.gettempdir(),
                                f"chapter_{current.chapter_number}_outline.json")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(current.__dict__, f, ensure_ascii=False, indent=2, default=str)

        print(f"  文件路径：{tmp_path}")

        # 用系统默认编辑器打开
        try:
            os.startfile(tmp_path)
        except AttributeError:
            # 非 Windows 系统
            import subprocess
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", tmp_path],
                          check=False)

        input("  编辑完成并保存后，按回车继续...")

        # 重新加载
        try:
            with open(tmp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 保留 chapter_number（不能改）
            data["chapter_number"] = current.chapter_number
            updated = self._dict_to_outline(data, current.chapter_number)
            print_success("手动编辑已载入！")
            self._display_outline(updated)
            return updated
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print_warning(f"读取编辑结果失败: {e}，保留原细纲")
            return current

    # ── 展示 ───────────────────────────────────────────────

    def _wrap(self, text: str, indent: int = 2, first_indent: str = None) -> str:
        """终端自动换行，宽度72与分割线一致"""
        prefix = " " * indent
        return textwrap.fill(
            text, width=72,
            initial_indent=first_indent if first_indent is not None else prefix,
            subsequent_indent=prefix,
        )

    def _display_outline(self, outline: ChapterOutline):
        """展示细纲 — 任务式呈现"""
        print_header(f"第 {outline.chapter_number} 章：{outline.chapter_title}")

        # 概览
        if outline.chapter_objective:
            print(self._wrap(f"本章目标：{outline.chapter_objective}"))
        if outline.volume_reference:
            print(self._wrap(f"呼应粗纲：{outline.volume_reference}"))

        # 场景
        if outline.scenes:
            print_subheader("场景流程")
            for i, scene in enumerate(outline.scenes, 1):
                if isinstance(scene, dict):
                    loc = scene.get("location", "?")
                    purpose = scene.get("purpose", "")
                    summary = scene.get("summary", "")
                    words = scene.get("estimated_words", "?")
                    print(self._wrap(f"场景{i} [{loc}]（约{words}字）— {purpose}"))
                    if summary:
                        print(self._wrap(summary, indent=8))

        # ★ 任务清单（核心）
        print_subheader("任务清单")

        task_groups = [
            ("人物信息更新", "character_updates", "👤"),
            ("伏笔设下", "foreshadowing_plant", "📌"),
            ("伏笔回收", "foreshadowing_recover", "✅"),
            ("结尾钩子", "hooks_set", "🪝"),
            ("世界观信息释放", "world_building_revealed", "🌍"),
            ("冲突推进", "conflicts_advanced", "⚔️"),
        ]

        for label, attr, emoji in task_groups:
            tasks = getattr(outline, attr, [])
            if tasks:
                print(f"\n  {emoji} {label}：")
                for t in tasks:
                    # ☐ 加在每段前面，后续行对齐"    ☐ "之后
                    print(self._wrap(f"☐ {t}", indent=6, first_indent="    ☐ "))

        # 写作注意事项
        if outline.writing_notes:
            print_subheader("写作注意事项")
            for line in outline.writing_notes.strip().split("\n"):
                if line.strip():
                    print(self._wrap(line.strip()))
                else:
                    print()

        # 出场信息
        if outline.characters_appearing:
            print_section(f"出场人物：{'、'.join(outline.characters_appearing)}")
        if outline.locations:
            print_section(f"出场地点：{'、'.join(outline.locations)}")

    # ── 辅助 ───────────────────────────────────────────────

    def _dict_to_outline(self, data: dict, chapter_number: int) -> ChapterOutline:
        """dict → ChapterOutline"""
        return ChapterOutline(
            chapter_number=chapter_number,
            chapter_title=data.get("chapter_title", f"第{chapter_number}章"),
            volume_reference=data.get("volume_reference", ""),
            chapter_objective=data.get("chapter_objective", ""),
            scenes=data.get("scenes", []),
            character_updates=data.get("character_updates", []),
            foreshadowing_plant=data.get("foreshadowing_plant", []),
            foreshadowing_recover=data.get("foreshadowing_recover", []),
            hooks_set=data.get("hooks_set", []),
            world_building_revealed=data.get("world_building_revealed", []),
            conflicts_advanced=data.get("conflicts_advanced", []),
            characters_appearing=data.get("characters_appearing", []),
            locations=data.get("locations", []),
            pacing_type=data.get("pacing_type", ""),
            writing_notes=data.get("writing_notes", ""),
        )

    def _extract_relevant_section(
        self, volume_outline: VolumeOutline, chapter_number: int
    ) -> str:
        """
        从卷粗纲中提取与本章相关的部分。
        由于粗纲是叙事文本，返回完整粗纲 + 标注本章大致对应位置。
        """
        # 尝试从 chapter_range 推算本章在粗纲中的位置
        total_chapters = self.config.chapters_per_volume
        position_ratio = chapter_number / max(total_chapters, 1)

        stages = ["开头", "前期", "中期", "中后期", "结尾"]
        stage_idx = min(int(position_ratio * len(stages)), len(stages) - 1)
        stage = stages[stage_idx]

        header = f"本卷粗纲（第 {chapter_number} 章大致位于本卷的「{stage}」阶段）"

        pacing_block = ""
        if getattr(volume_outline, "pacing_plan", ""):
            pacing_block = f"""
【本卷张弛节奏表（本章节奏类型的判定依据）】
{volume_outline.pacing_plan}
"""

        return f"""{header}

【完整粗纲】
{volume_outline.narrative_outline}
{pacing_block}
【本卷关键事件】
{json.dumps(volume_outline.key_events, ensure_ascii=False)}

【本卷伏笔计划】
设下：{json.dumps(volume_outline.foreshadowing_planted, ensure_ascii=False)}
回收：{json.dumps(volume_outline.foreshadowing_recovered, ensure_ascii=False)}
"""

    def get_chapter_task_summary(self, outline: ChapterOutline) -> str:
        """将细纲转为任务摘要（供后续 Agent 使用）"""
        tasks = []
        if outline.pacing_type:
            tasks.append(f"节奏：{outline.pacing_type}章")
        if outline.chapter_objective:
            tasks.append(f"目标：{outline.chapter_objective}")
        if outline.character_updates:
            tasks.append(f"角色变更：{'；'.join(outline.character_updates)}")
        if outline.foreshadowing_plant:
            tasks.append(f"伏笔设下：{'；'.join(outline.foreshadowing_plant)}")
        if outline.foreshadowing_recover:
            tasks.append(f"伏笔回收：{'；'.join(outline.foreshadowing_recover)}")
        if outline.hooks_set:
            tasks.append(f"钩子：{'；'.join(outline.hooks_set)}")
        if outline.conflicts_advanced:
            tasks.append(f"冲突：{'；'.join(outline.conflicts_advanced)}")
        return " | ".join(tasks)
