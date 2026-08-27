"""
项目管理 — JSON 文件持久化，项目创建/保存/加载
"""

import os
import json
import config as app_config
from models import (
    ProjectConfig, SettingLibrary, VolumeOutline, ChapterOutline,
    ChapterSummary, ProjectState,
)
from utils import to_dict, save_json, load_json, print_success, print_error


class ProjectManager:
    """管理项目的持久化存储"""

    def __init__(self, project_name: str = ""):
        self.project_name = project_name
        self.project_dir = ""
        self.state: ProjectState | None = None
        if project_name:
            self.project_dir = os.path.join(app_config.DATA_DIR, self._safe_name(project_name))

    @staticmethod
    def _safe_name(name: str) -> str:
        """将项目名转为安全的目录名"""
        safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)
        return safe.strip() or "untitled"

    # ── 创建 & 加载 ────────────────────────────────────────

    def create_new(self, config: ProjectConfig) -> ProjectState:
        """创建新项目"""
        self.project_name = config.project_name
        self.project_dir = os.path.join(app_config.DATA_DIR, self._safe_name(config.project_name))
        os.makedirs(self.project_dir, exist_ok=True)

        self.state = ProjectState(config=config)
        self._save_all()
        print_success(f"项目「{config.project_name}」已创建 → {self.project_dir}")
        return self.state

    def load(self, project_name: str) -> ProjectState | None:
        """加载已有项目"""
        self.project_name = project_name
        self.project_dir = os.path.join(app_config.DATA_DIR, self._safe_name(project_name))

        if not os.path.exists(self.project_dir):
            print_error(f"项目「{project_name}」不存在")
            return None

        # 加载各部分数据
        config_data = load_json(os.path.join(self.project_dir, "config.json"))
        setting_data = load_json(os.path.join(self.project_dir, "setting_library.json"))
        volumes_data = load_json(os.path.join(self.project_dir, "volume_outlines.json"))
        chapters_data = load_json(os.path.join(self.project_dir, "chapter_outlines.json"))
        summaries_data = load_json(os.path.join(self.project_dir, "chapter_summaries.json"))
        contents_data = load_json(os.path.join(self.project_dir, "chapter_contents.json"))
        meta_data = load_json(os.path.join(self.project_dir, "meta.json"))

        if not config_data:
            print_error("项目数据损坏，缺少 config.json")
            return None

        self.state = ProjectState(
            config=ProjectConfig(**config_data) if config_data else ProjectConfig(),
            setting_library=self._dict_to_setting_library(setting_data or {}),
            volume_outlines=volumes_data or {},
            chapter_outlines=chapters_data or {},
            chapter_summaries=summaries_data or {},
            chapter_contents=contents_data or {},
            current_volume=meta_data.get("current_volume", 1) if meta_data else 1,
            current_chapter=meta_data.get("current_chapter", 1) if meta_data else 1,
            unresolved_hooks_global=meta_data.get("unresolved_hooks_global", []) if meta_data else [],
        )
        print_success(f"项目「{project_name}」加载成功")
        print(f"  当前进度：第 {self.state.current_volume} 卷 · 第 {self.state.current_chapter} 章")
        return self.state

    # ── 保存 ────────────────────────────────────────────────

    def _save_all(self):
        """保存所有数据"""
        if not self.state:
            return
        os.makedirs(self.project_dir, exist_ok=True)
        save_json(to_dict(self.state.config), os.path.join(self.project_dir, "config.json"))
        save_json(to_dict(self.state.setting_library), os.path.join(self.project_dir, "setting_library.json"))
        save_json(self.state.volume_outlines, os.path.join(self.project_dir, "volume_outlines.json"))
        save_json(self.state.chapter_outlines, os.path.join(self.project_dir, "chapter_outlines.json"))
        save_json(self.state.chapter_summaries, os.path.join(self.project_dir, "chapter_summaries.json"))
        save_json(self.state.chapter_contents, os.path.join(self.project_dir, "chapter_contents.json"))
        save_json({
            "project_name": self.project_name,
            "current_volume": self.state.current_volume,
            "current_chapter": self.state.current_chapter,
            "unresolved_hooks_global": self.state.unresolved_hooks_global,
        }, os.path.join(self.project_dir, "meta.json"))

    def save_setting_library(self):
        """单独保存设定库"""
        if self.state:
            save_json(to_dict(self.state.setting_library),
                      os.path.join(self.project_dir, "setting_library.json"))

    def save_volume_outline(self, volume_number: int):
        """保存指定卷的粗纲"""
        if self.state:
            # 更新 volumes dict
            self.state.volume_outlines[str(volume_number)] = to_dict(
                self._get_volume(volume_number)
            )
            save_json(self.state.volume_outlines,
                      os.path.join(self.project_dir, "volume_outlines.json"))

    def save_chapter_outline(self, chapter_number: int):
        """保存指定章的细纲"""
        if self.state:
            self.state.chapter_outlines[str(chapter_number)] = to_dict(
                self._get_chapter(chapter_number)
            )
            save_json(self.state.chapter_outlines,
                      os.path.join(self.project_dir, "chapter_outlines.json"))

    def save_chapter_content(self, chapter_number: int, content):
        """保存章节正文"""
        if self.state:
            key = str(chapter_number)
            if hasattr(content, "__dict__"):
                self.state.chapter_contents[key] = to_dict(content)
            else:
                self.state.chapter_contents[key] = content
            save_json(self.state.chapter_contents,
                      os.path.join(self.project_dir, "chapter_contents.json"))

    def save_chapter_summaries(self):
        """保存全部章节摘要"""
        if self.state:
            save_json(self.state.chapter_summaries,
                      os.path.join(self.project_dir, "chapter_summaries.json"))

    def get_chapter_content(self, chapter_number: int):
        """获取章节正文"""
        if self.state:
            key = str(chapter_number)
            data = self.state.chapter_contents.get(key)
            if data:
                from models import ChapterContent
                if isinstance(data, ChapterContent):
                    return data
                return ChapterContent(**data)
        return None

    def save_meta(self):
        """保存元数据"""
        if self.state:
            save_json({
                "project_name": self.project_name,
                "current_volume": self.state.current_volume,
                "current_chapter": self.state.current_chapter,
                "unresolved_hooks_global": self.state.unresolved_hooks_global,
            }, os.path.join(self.project_dir, "meta.json"))

    # ── 伏笔台账持久化 ─────────────────────────────────────

    def save_registry(self, registry):
        """保存伏笔台账，并同步 meta 的未回收伏笔摘要（兼容旧读取方）"""
        if not self.state:
            return
        save_json(registry.to_dict(),
                  os.path.join(self.project_dir, "foreshadow_registry.json"))
        self.state.unresolved_hooks_global = registry.open_texts()
        self.save_meta()

    def load_registry(self):
        """加载伏笔台账（不存在则返回空台账）"""
        from foreshadow_registry import ForeshadowRegistry
        data = load_json(os.path.join(self.project_dir, "foreshadow_registry.json"))
        return ForeshadowRegistry.from_dict(data or {})

    # ── 辅助 ────────────────────────────────────────────────

    def _get_volume(self, vol_num: int):
        """获取卷粗纲对象（如果存在）"""
        key = str(vol_num)
        if key in self.state.volume_outlines:
            data = self.state.volume_outlines[key]
            if isinstance(data, VolumeOutline):
                return data
            return VolumeOutline(**data)
        return None

    def _get_chapter(self, ch_num: int):
        """获取章细纲对象（如果存在）"""
        key = str(ch_num)
        if key in self.state.chapter_outlines:
            data = self.state.chapter_outlines[key]
            if isinstance(data, ChapterOutline):
                return data
            return ChapterOutline(**data)
        return None

    @staticmethod
    def _dict_to_setting_library(data: dict) -> SettingLibrary:
        """从 dict 恢复 SettingLibrary"""
        from models import (
            CharacterEntry, GeographyEntry, HistoryEntry,
            PowerSystemEntry, FactionEntry, ItemEntry
        )
        lib = SettingLibrary()
        for key, val in data.get("characters", {}).items():
            lib.characters[key] = CharacterEntry(**val) if isinstance(val, dict) else val
        for key, val in data.get("geography", {}).items():
            lib.geography[key] = GeographyEntry(**val) if isinstance(val, dict) else val
        for key, val in data.get("history", {}).items():
            lib.history[key] = HistoryEntry(**val) if isinstance(val, dict) else val
        for key, val in data.get("power_system", {}).items():
            lib.power_system[key] = PowerSystemEntry(**val) if isinstance(val, dict) else val
        for key, val in data.get("factions", {}).items():
            lib.factions[key] = FactionEntry(**val) if isinstance(val, dict) else val
        for key, val in data.get("items", {}).items():
            lib.items[key] = ItemEntry(**val) if isinstance(val, dict) else val
        return lib

    # ── 列表 ────────────────────────────────────────────────

    @staticmethod
    def list_projects() -> list:
        """列出所有已保存的项目（含详细信息以便区分）"""
        if not os.path.exists(app_config.DATA_DIR):
            return []
        projects = []
        for name in os.listdir(app_config.DATA_DIR):
            path = os.path.join(app_config.DATA_DIR, name)
            if os.path.isdir(path):
                meta = load_json(os.path.join(path, "meta.json"))
                config = load_json(os.path.join(path, "config.json"))
                if meta:
                    proj = {
                        "name": meta.get("project_name", name),
                        "dir_name": name,
                        "volume": meta.get("current_volume", 1),
                        "chapter": meta.get("current_chapter", 1),
                    }
                    if config:
                        proj["genre"] = config.get("genre", "?")
                        proj["core_idea"] = config.get("core_idea", "")[:60]
                        proj["writing_style"] = config.get("writing_style", "")
                    projects.append(proj)
        return projects
