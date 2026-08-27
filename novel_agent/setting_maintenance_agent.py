"""
数据库维护 Agent — 从已完成章节中提取设定更新
"""

import json
from models import ChapterOutline, ChapterContent, ProjectConfig, SettingLibrary, CharacterEntry, GeographyEntry
from api_client import get_client
from prompts import SETTING_MAINTENANCE_SYSTEM, get_setting_maintenance_user
from utils import print_success, print_warning

# 复用现有的 setting_library.py 中的 SettingLibraryManager 与死亡判定助手
from setting_library import SettingLibraryManager, is_dead, RECALL_CONTEXT_KEYWORDS


class SettingMaintenanceAgent:
    """数据库维护 Agent — 从已完成章节中智能提取设定更新"""

    def __init__(self, config: ProjectConfig, setting_library: SettingLibrary):
        self.config = config
        self.setting_library = setting_library
        self.slm = SettingLibraryManager(setting_library, config)

    def update_from_chapter(
        self,
        chapter_content: ChapterContent,
        chapter_outline: ChapterOutline,
        chapter_number: int,
    ) -> dict:
        """
        从已完成的章节正文和细纲中提取设定更新。

        Returns:
            dict: {"new_entries": N, "updates": N, "issues": [...]}
        """
        print(f"\n  🔍 AI 正在从第 {chapter_number} 章正文中提炼设定更新...")

        outline_json = json.dumps(chapter_outline.__dict__, ensure_ascii=False, indent=2, default=str)
        current_summary = self.slm.get_summary(
            current_chapter=chapter_number,
            unresolved_hooks=[],  # 伏笔保护在 get_summary 中处理
        )

        client = get_client()
        user_prompt = get_setting_maintenance_user(
            chapter_number=chapter_number,
            chapter_content=chapter_content.content,
            chapter_outline_json=outline_json,
            current_settings_summary=current_summary,
        )

        result = None
        last_error = ""
        # 维护输出是五库全量条目的长 JSON，flash 偶发解析失败：先完整重试一次再降级
        for _ in range(2):
            try:
                result = client.chat_with_json_output(
                    SETTING_MAINTENANCE_SYSTEM, user_prompt,
                    temperature=0.4, max_tokens=16384,
                )
                if "_parse_error" not in result:
                    break
                last_error = "JSON 解析失败"
                result = None
            except Exception as e:
                last_error = str(e)
                result = None

        if result is None:
            print_warning(f"设定更新连续失败（{last_error}），回退到简单入库")
            fallback_added = self._simple_auto_add(chapter_outline, chapter_number)
            return {"new_entries": fallback_added, "updates": 0, "issues": [f"AI 维护失败，简单入库 {fallback_added} 条: {last_error}"]}

        # ── 处理新条目 ──
        new_entries = result.get("new_entries", {})
        entry_added = 0
        for lib_name in ["characters", "geography", "history", "power_system", "factions", "items"]:
            entries = new_entries.get(lib_name, {})
            for entry_name, entry_data in entries.items():
                if not entry_data or not isinstance(entry_data, dict):
                    continue
                if self.slm.entry_exists(lib_name, entry_name):
                    continue
                try:
                    if lib_name == "characters":
                        entry_data.setdefault("first_appearance_chapter", chapter_number)
                    elif lib_name in ("geography", "factions", "items"):
                        entry_data.setdefault("first_mentioned_chapter", chapter_number)
                    elif lib_name == "history":
                        entry_data.setdefault("revealed_in_chapter", chapter_number)
                    elif lib_name == "power_system":
                        entry_data.setdefault("first_explained_chapter", chapter_number)
                    self.slm.add_entry(lib_name, entry_name, **entry_data)
                    entry_added += 1
                except Exception as e:
                    print_warning(f"添加条目「{entry_name}」失败: {e}")

        # ── 处理已有条目更新 ──
        updates = result.get("updates", {})
        update_count = 0
        for lib_name in ["characters", "factions", "geography", "history", "power_system", "items"]:
            lib_updates = updates.get(lib_name, {})
            for entry_name, fields in lib_updates.items():
                if not isinstance(fields, dict):
                    continue
                if self.slm.entry_exists(lib_name, entry_name):
                    try:
                        self.slm.update_entry(lib_name, entry_name, **fields)
                        update_count += 1
                    except Exception as e:
                        print_warning(f"更新条目「{entry_name}」失败: {e}")

        # ── 自动刷新活跃窗口时间戳 ──
        active_bump = 0
        for name in chapter_outline.characters_appearing:
            if name and self.slm.entry_exists("characters", name):
                self.slm.update_entry("characters", name, last_active_chapter=chapter_number)
                active_bump += 1
        for name in chapter_outline.locations:
            if name and self.slm.entry_exists("geography", name):
                self.slm.update_entry("geography", name, last_active_chapter=chapter_number)
                active_bump += 1

        # ── 显示结果 ──
        if entry_added > 0:
            print_success(f"新增 {entry_added} 个设定条目")
        if update_count > 0:
            print_success(f"更新 {update_count} 个已有条目")
        if active_bump > 0:
            print(f"  🔄 刷新 {active_bump} 个条目的活跃时间戳")

        # ── 显示矛盾 ──
        issues = result.get("consistency_issues", [])

        # ── 写后守卫：本地检测已死亡角色在正文中出场（非回忆语境） ──
        dead_revivals = self._detect_dead_revival(chapter_content.content)
        for name in dead_revivals:
            msg = f"疑似剧情矛盾：已死亡/退场角色「{name}」在本章正文中出场（非回忆/闪回语境），请核实"
            issues.insert(0, msg)

        if issues:
            print_warning(f"AI 发现 {len(issues)} 个潜在矛盾：")
            for issue in issues:
                print(f"    · {issue}")

        return {
            "new_entries": entry_added,
            "updates": update_count,
            "issues": issues,
        }

    def _detect_dead_revival(self, content: str) -> list:
        """写后守卫：已死亡/退场角色是否在正文中出场。

        逐次出现检查上下文（前后 40 字）：只要有一次出现不在回忆/闪回/遗物等
        语境中，即判定为疑似矛盾（宽松策略：全部出现都在回忆语境则放行）。
        """
        if not content:
            return []
        hits = []
        for name, entry in self.setting_library.characters.items():
            if not name or len(name) < 2:
                continue
            if not is_dead(getattr(entry, "current_status", "") or ""):
                continue
            start = 0
            while True:
                idx = content.find(name, start)
                if idx < 0:
                    break
                window = content[max(0, idx - 40): idx + len(name) + 40]
                if not any(k in window for k in RECALL_CONTEXT_KEYWORDS):
                    hits.append(name)
                    break
                start = idx + len(name)
        return hits

    def _simple_auto_add(self, outline: ChapterOutline, chapter_number: int):
        """简单自动入库（AI 失败时的回退方案）"""
        added = 0
        for name in outline.characters_appearing:
            if name and name not in self.setting_library.characters:
                self.setting_library.characters[name] = CharacterEntry(
                    name=name, first_appearance_chapter=chapter_number,
                    notes="（待 AI 完善）",
                )
                added += 1
        for name in outline.locations:
            if name and name not in self.setting_library.geography:
                self.setting_library.geography[name] = GeographyEntry(
                    name=name, first_mentioned_chapter=chapter_number,
                    notes="（待 AI 完善）",
                )
                added += 1
        if added > 0:
            print_success(f"简单入库 {added} 个条目（待后续 AI 完善）")
        return added
