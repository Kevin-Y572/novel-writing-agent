"""
粗纲 Agent — 分卷叙事化大纲生成 + 多轮修改
"""

import json
import textwrap
from models import VolumeOutline, ProjectConfig, SettingLibrary
from api_client import get_client
from prompts import ROUGH_OUTLINE_SYSTEM, get_rough_outline_user
from utils import print_header, print_subheader, print_section
from utils import print_success, print_warning, ask, ask_yes_no


class RoughOutlineAgent:
    """粗纲生成 Agent"""

    def __init__(self, config: ProjectConfig, setting_library: SettingLibrary):
        self.config = config
        self.setting_library = setting_library

    def generate_volume_outline(
        self,
        volume_number: int,
        setting_summary: str,
        previous_summary: str = "",
        unresolved_hooks: str = "",
    ) -> VolumeOutline:
        """
        生成单卷粗纲。
        返回 VolumeOutline 对象，失败时返回 None。
        """
        is_first = (volume_number == 1)

        print_header(f"第 {volume_number} 卷粗纲生成")
        print("  正在调用 AI 生成叙事化粗纲...（可能需要 30-90 秒）")
        if is_first:
            print("  本卷为第一卷，将包含「前5章背景释放计划」")

        client = get_client()
        user_prompt = get_rough_outline_user(
            volume_number=volume_number,
            genre=self.config.genre,
            writing_style=self.config.writing_style,
            internet_slang_level=self.config.internet_slang_level,
            narrative_person=self.config.narrative_person,
            core_idea=self.config.core_idea,
            setting_summary=setting_summary,
            previous_summary=previous_summary,
            unresolved_hooks=unresolved_hooks,
            chapters_per_volume=self.config.chapters_per_volume,
            is_first_volume=is_first,
        )

        result = client.chat_with_json_output(
            ROUGH_OUTLINE_SYSTEM, user_prompt,
            temperature=0.8, max_tokens=16384,
        )

        if "_parse_error" in result:
            print_warning("AI 返回的 JSON 无法解析，将显示原始文本供你参考")
            print(f"\n{result.get('_raw', '')[:2000]}...")
            return None

        outline = self._dict_to_outline(result, volume_number)
        self._display_outline(outline)
        return outline

    def revise_outline(
        self,
        current_outline: VolumeOutline,
        feedback: str,
        setting_summary: str,
        previous_summary: str = "",
        unresolved_hooks: str = "",
    ) -> VolumeOutline:
        """根据用户反馈修改粗纲"""
        print("\n  正在根据你的反馈修改粗纲...")

        client = get_client()
        revision_prompt = f"""请根据以下作者反馈，修改第 {current_outline.volume_number} 卷的粗纲。

【当前粗纲】
{json.dumps(current_outline.__dict__, ensure_ascii=False, indent=2, default=str)}

【设定库概要】
{setting_summary}

【前卷摘要】
{previous_summary if previous_summary else "（无）"}

【未回收伏笔】
{unresolved_hooks if unresolved_hooks else "（无）"}

【作者修改意见】
{feedback}

请输出修改后的完整粗纲 JSON（格式与原来相同），直接输出 JSON。"""

        result = client.chat_with_json_output(ROUGH_OUTLINE_SYSTEM, revision_prompt,
                                               temperature=0.7, max_tokens=16384)

        if "_parse_error" in result:
            print_warning("修改失败，JSON 无法解析")
            return current_outline

        outline = self._dict_to_outline(result, current_outline.volume_number)
        print_success("修改完成！")
        self._display_outline(outline)
        return outline

    def interactive_outline_loop(
        self,
        volume_number: int,
        setting_summary: str,
        previous_summary: str = "",
        unresolved_hooks: str = "",
    ) -> VolumeOutline:
        """
        交互式粗纲生成循环：
        生成 → 展示 → 用户反馈 → 修改 → 再展示 → 确认
        """
        outline = self.generate_volume_outline(
            volume_number, setting_summary,
            previous_summary, unresolved_hooks,
        )

        if outline is None:
            if not ask_yes_no("生成失败，是否重试？", default="y"):
                return None
            outline = self.generate_volume_outline(
                volume_number, setting_summary,
                previous_summary, unresolved_hooks,
            )
            if outline is None:
                print_warning("生成再次失败，请检查 API 配置或调整输入")
                return None

        while True:
            print()
            action = input("  [确认(y) / 修改意见 / 重新生成(r) / 手动编辑(e)]: ").strip()

            if action.lower() in ("y", "yes", "确认", ""):
                print_success(f"第 {volume_number} 卷粗纲已确认！")
                return outline

            elif action.lower() in ("r", "重新生成", "重试"):
                outline = self.generate_volume_outline(
                    volume_number, setting_summary,
                    previous_summary, unresolved_hooks,
                )
                if outline is None:
                    continue

            elif action.lower() in ("e", "编辑", "手动"):
                outline = self._manual_edit(outline)
                self._display_outline(outline)

            elif action.strip():
                # 视为修改意见
                outline = self.revise_outline(
                    outline, action, setting_summary,
                    previous_summary, unresolved_hooks,
                )
                if outline is None:
                    continue

    # ── 辅助方法 ───────────────────────────────────────────

    def _manual_edit(self, current: VolumeOutline) -> VolumeOutline:
        """手动编辑：导出粗纲为 JSON，用系统默认编辑器打开"""
        import os
        import sys
        import tempfile

        print("\n  [手动编辑模式] 即将在编辑器中打开当前粗纲的 JSON 文件")
        print("  修改后保存并关闭编辑器即可")

        tmp_path = os.path.join(tempfile.gettempdir(),
                                f"volume_{current.volume_number}_outline.json")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(current.__dict__, f, ensure_ascii=False, indent=2, default=str)

        print(f"  文件路径：{tmp_path}")

        try:
            os.startfile(tmp_path)
        except AttributeError:
            import subprocess
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", tmp_path],
                          check=False)

        input("  编辑完成并保存后，按回车继续...")

        try:
            with open(tmp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["volume_number"] = current.volume_number
            updated = self._dict_to_outline(data, current.volume_number)
            print_success("手动编辑已载入！")
            return updated
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print_warning(f"读取编辑结果失败: {e}，保留原粗纲")
            return current

    def _display_outline(self, outline: VolumeOutline):
        """展示粗纲"""
        print_header(f"第 {outline.volume_number} 卷：{outline.volume_title}")
        print(f"  章节范围：{outline.chapter_range}")

        print_subheader("行文脉络（叙事化粗纲）")
        # 自动换行，终端友好
        for line in outline.narrative_outline.split("\n"):
            if line.strip():
                print(textwrap.fill(line.strip(), width=72, initial_indent="", subsequent_indent=""))
            else:
                print()

        if outline.background_release_plan:
            print_subheader("前5章背景释放计划")
            for line in outline.background_release_plan.split("\n"):
                if line.strip():
                    print(textwrap.fill(line.strip(), width=72, initial_indent="", subsequent_indent=""))
                else:
                    print()

        if outline.main_conflicts:
            print_section("主要冲突")
            for c in outline.main_conflicts:
                print(f"    · {c}")

        if outline.key_events:
            print_section("关键事件节点")
            for e in outline.key_events:
                print(f"    · {e}")

        if outline.foreshadowing_planted:
            print_section("本卷设下伏笔")
            for f in outline.foreshadowing_planted:
                print(f"    · {f}")

        if outline.foreshadowing_recovered:
            print_section("本卷回收伏笔")
            for f in outline.foreshadowing_recovered:
                print(f"    · {f}")

        if outline.volume_ending_hook:
            print_section("卷末钩子")
            print(f"    {outline.volume_ending_hook}")

    def _dict_to_outline(self, data: dict, volume_number: int) -> VolumeOutline:
        """将 AI 返回的 dict 转为 VolumeOutline"""
        return VolumeOutline(
            volume_number=volume_number,
            volume_title=data.get("volume_title", f"第{volume_number}卷"),
            chapter_range=data.get("chapter_range", ""),
            narrative_outline=data.get("narrative_outline", ""),
            main_conflicts=data.get("main_conflicts", []),
            character_arcs=data.get("character_arcs", {}),
            foreshadowing_planted=data.get("foreshadowing_planted", []),
            foreshadowing_recovered=data.get("foreshadowing_recovered", []),
            key_events=data.get("key_events", []),
            volume_ending_hook=data.get("volume_ending_hook", ""),
            background_release_plan=data.get("background_release_plan", ""),
            author_notes=data.get("author_notes", ""),
        )
