"""
设定库管理 — 6 个子库的 AI 生成 + CRUD + 百度百科展示 + 一致性校验
"""

import json
from dataclasses import fields as dataclass_fields
from models import (
    SettingLibrary, CharacterEntry, GeographyEntry, HistoryEntry,
    PowerSystemEntry, FactionEntry, ItemEntry, ProjectConfig,
)
from api_client import get_client
from prompts import SETTING_INIT_SYSTEM, get_setting_init_user

# ── 死亡/退场判定（用于前后章节情节一致性守卫） ──────────────
DEAD_KEYWORDS = ("死亡", "身亡", "已死", "陨落", "阵亡", "逝世", "被杀", "丧命", "已故", "退场")
RECALL_CONTEXT_KEYWORDS = (
    "回忆", "闪回", "梦", "当年", "往昔", "生前", "想起", "记忆",
    "遗言", "遗体", "坟墓", "葬礼", "牌位", "遗物", "传说", "复活", "幻象", "往日",
    # 转世/重生类合法剧情（死人以新身份出场不算违规）
    "转世", "转生", "重生", "夺舍", "附身", "轮回", "化身", "前世",
)


def is_dead(status_text: str) -> bool:
    """根据 current_status 文本判断角色是否已死亡/退场（假死/濒死不算）"""
    if not status_text:
        return False
    if "假死" in status_text or "濒死" in status_text:
        return False
    return any(k in status_text for k in DEAD_KEYWORDS)


class SettingLibraryManager:
    """设定库管理器"""

    def __init__(self, library: SettingLibrary, config: ProjectConfig):
        self.lib = library
        self.config = config

    # ═══════════════════════════════════════════════════════
    # AI 初始化
    # ═══════════════════════════════════════════════════════

    def generate_initial_settings(self) -> dict:
        """调用 AI 生成初始设定库，返回解析后的 dict"""
        client = get_client()
        user_prompt = get_setting_init_user(
            core_idea=self.config.core_idea,
            core_setting=self.config.core_setting,
            genre=self.config.genre,
            author_notes=self.config.author_notes,
        )

        print("  正在调用 AI 生成初始设定库...（可能需要 30-60 秒）")
        result = client.chat_with_json_output(SETTING_INIT_SYSTEM, user_prompt,
                                               temperature=0.7, max_tokens=16384)

        if "_parse_error" in result:
            print(f"  ⚠ JSON 解析失败，使用原始文本\n  {result.get('_raw', '')[:500]}...")
            return {}

        # 将 dict 转为 dataclass
        self._merge_ai_result(result)
        return result

    @staticmethod
    def _build_entry(entry_cls, name: str, data: dict):
        """按 dataclass 字段过滤 LLM 返回的键——模型多返回/拼错字段名时忽略而非崩溃"""
        valid = {f.name for f in dataclass_fields(entry_cls)}
        kwargs = {k: v for k, v in data.items() if k in valid and k != "name"}
        return entry_cls(name=name, **kwargs)

    def _merge_ai_result(self, data: dict):
        """将 AI 返回的 dict 合并到 SettingLibrary"""
        char_data = data.get("characters", {})
        geo_data = data.get("geography", {})
        hist_data = data.get("history", {})
        power_data = data.get("power_system", {})
        faction_data = data.get("factions", {})
        item_data = data.get("items", {})

        for name, entry in char_data.items():
            if isinstance(entry, dict):
                self.lib.characters[name] = self._build_entry(CharacterEntry, name, entry)
        for name, entry in geo_data.items():
            if isinstance(entry, dict):
                self.lib.geography[name] = self._build_entry(GeographyEntry, name, entry)
        for name, entry in hist_data.items():
            if isinstance(entry, dict):
                self.lib.history[name] = self._build_entry(HistoryEntry, name, entry)
        for name, entry in power_data.items():
            if isinstance(entry, dict):
                self.lib.power_system[name] = self._build_entry(PowerSystemEntry, name, entry)
        for name, entry in faction_data.items():
            if isinstance(entry, dict):
                self.lib.factions[name] = self._build_entry(FactionEntry, name, entry)
        for name, entry in item_data.items():
            if isinstance(entry, dict):
                self.lib.items[name] = self._build_entry(ItemEntry, name, entry)

    # ═══════════════════════════════════════════════════════
    # CRUD 操作
    # ═══════════════════════════════════════════════════════

    # ── 通用 ──────────────────────────────────────────────

    def get_library_names(self) -> list:
        return ["characters", "geography", "history", "power_system", "factions", "items"]

    def get_library_label(self, name: str) -> str:
        labels = {
            "characters": "人物库",
            "geography": "地理库",
            "history": "历史库",
            "power_system": "战力设定库",
            "factions": "势力分布库",
            "items": "道具库",
        }
        return labels.get(name, name)

    def get_entries(self, library_name: str) -> dict:
        return getattr(self.lib, library_name, {})

    def get_entry_count(self, library_name: str) -> int:
        return len(self.get_entries(library_name))

    def entry_exists(self, library_name: str, entry_name: str) -> bool:
        return entry_name in self.get_entries(library_name)

    def add_entry(self, library_name: str, entry_name: str, **kwargs):
        """添加条目（手动或程序化），自动处理 AI 返回字段名不匹配的问题"""
        entries = self.get_entries(library_name)
        entry_class = {
            "characters": CharacterEntry,
            "geography": GeographyEntry,
            "history": HistoryEntry,
            "power_system": PowerSystemEntry,
            "factions": FactionEntry,
            "items": ItemEntry,
        }.get(library_name)

        if entry_class is None:
            raise ValueError(f"未知的设定库类型: {library_name}")

        kwargs["name"] = entry_name

        # 字段映射：AI 返回的通用字段名 → 模型特定字段名
        if entry_class == CharacterEntry and "description" in kwargs:
            if not kwargs.get("background"):
                kwargs["background"] = kwargs.pop("description")
        if entry_class == PowerSystemEntry and "description" in kwargs:
            if not kwargs.get("basic_info"):
                kwargs["basic_info"] = kwargs.pop("description")

        # 过滤掉模型不认识的字段，避免 __init__() got unexpected keyword argument
        valid_fields = {f.name for f in entry_class.__dataclass_fields__.values()}
        filtered_kwargs = {k: v for k, v in kwargs.items() if k in valid_fields}

        entries[entry_name] = entry_class(**filtered_kwargs)

    def update_entry(self, library_name: str, entry_name: str, **kwargs):
        """更新条目字段（自动过滤模型不认识的字段）"""
        entries = self.get_entries(library_name)
        if entry_name not in entries:
            raise KeyError(f"条目「{entry_name}」不存在于 {library_name}")
        entry = entries[entry_name]
        for key, val in kwargs.items():
            # 只更新模型已有的字段，静默跳过不认识的
            if hasattr(entry, key):
                setattr(entry, key, val)

    def remove_entry(self, library_name: str, entry_name: str):
        """删除条目"""
        entries = self.get_entries(library_name)
        if entry_name in entries:
            del entries[entry_name]

    def rename_entry(self, library_name: str, old_name: str, new_name: str):
        """重命名条目"""
        entries = self.get_entries(library_name)
        if old_name in entries:
            entries[new_name] = entries.pop(old_name)
            entries[new_name].name = new_name

    # ═══════════════════════════════════════════════════════
    # 百度百科风格展示
    # ═══════════════════════════════════════════════════════

    def display_entry(self, library_name: str, entry_name: str) -> str:
        """以百度百科风格格式化单个条目"""
        entries = self.get_entries(library_name)
        if entry_name not in entries:
            return f"[条目「{entry_name}」不存在]"

        entry = entries[entry_name]
        return self._format_as_baike(entry, library_name)

    def display_all_entries(self, library_name: str) -> str:
        """列出某个子库的所有条目名"""
        entries = self.get_entries(library_name)
        label = self.get_library_label(library_name)
        if not entries:
            return f"[{label}] 暂无条目"
        lines = [f"【{label}】（共 {len(entries)} 条）"]
        for i, name in enumerate(entries.keys(), 1):
            entry = entries[name]
            # 提取一行简介
            desc = getattr(entry, "description", "") or getattr(entry, "background", "") or ""
            desc = desc[:60].replace("\n", " ") + ("..." if len(desc) > 60 else "")
            lines.append(f"  [{i}] {name}" + (f" — {desc}" if desc else ""))
        return "\n".join(lines)

    def _format_as_baike(self, entry, lib_type: str) -> str:
        """百度百科词条格式"""
        lines = []
        lines.append(f"╔{'═'*68}╗")
        lines.append(f"║  {entry.name:^64}  ║")
        lines.append(f"╚{'═'*68}╝")

        # 根据类型提取不同字段
        field_map = {
            "characters": [
                ("性别", "gender"), ("年龄", "age"), ("别名", "aliases"),
                ("外貌", "appearance"), ("性格", "personality"),
                ("背景", "background"), ("能力", "abilities"),
                ("关系网", "relationships"), ("当前状态", "current_status"),
                ("首次出场", "first_appearance_chapter"), ("备注", "notes"),
            ],
            "geography": [
                ("类型", "type"), ("描述", "description"),
                ("重要性", "significance"), ("关联势力", "related_factions"),
                ("关联人物", "related_characters"),
                ("首次提及", "first_mentioned_chapter"), ("备注", "notes"),
            ],
            "history": [
                ("时期", "time_period"), ("描述", "description"),
                ("影响", "impact"), ("关联人物", "related_characters"),
                ("关联势力", "related_factions"),
                ("揭示章节", "revealed_in_chapter"), ("备注", "notes"),
            ],
            "power_system": [
                ("分类", "category"), ("境界列表", "levels"),
                ("基础设定", "basic_info"), ("高级设定", "advanced_info"),
                ("特殊情况", "special_cases"),
                ("首次解释章节", "first_explained_chapter"), ("备注", "notes"),
            ],
            "factions": [
                ("类型", "type"), ("描述", "description"),
                ("首领", "leader"), ("核心成员", "key_members"),
                ("势力范围", "territory"), ("势力关系", "relationships"),
                ("首次提及", "first_mentioned_chapter"), ("备注", "notes"),
            ],
            "items": [
                ("类型", "type"), ("描述", "description"),
                ("持有者", "owner"), ("当前状态", "current_status"),
                ("首次提及", "first_mentioned_chapter"), ("备注", "notes"),
            ],
        }

        for label, attr in field_map.get(lib_type, []):
            value = getattr(entry, attr, None)
            if value is not None and value != "" and value != [] and value != {}:
                formatted = self._format_value(value, label)
                lines.append(f"  ▸ {label}：{formatted}")

        return "\n".join(lines)

    def _format_value(self, value, label: str) -> str:
        if isinstance(value, list):
            if not value:
                return "（无）"
            items = [str(v) if not isinstance(v, dict) else
                     " | ".join(f"{k}: {v}" for k, v in v.items())
                     for v in value]
            return "\n    - " + "\n    - ".join(items)
        elif isinstance(value, dict):
            if not value:
                return "（无）"
            items = [f"{k} → {v}" for k, v in value.items()]
            return "\n    · " + "\n    · ".join(items)
        elif value is None:
            return "（未知）"
        else:
            return str(value).replace("\n", "\n    ")

    # ═══════════════════════════════════════════════════════
    # 一致性校验
    # ═══════════════════════════════════════════════════════

    def check_consistency(self) -> list:
        """
        校验设定库内部一致性，返回问题列表。
        检查项：
        - 人物所属势力是否在势力库中存在
        - 人物的能力是否对应战力体系的境界
        - 地理条目关联的势力是否存在
        - 历史事件关联的人物是否存在
        - 势力条目中的成员是否在人物库中
        - 道具条目的持有者是否在人物库中
        """
        issues = []

        # 获取所有名称集合
        char_names = set(self.lib.characters.keys())
        faction_names = set(self.lib.factions.keys())
        geo_names = set(self.lib.geography.keys())
        power_names = set(self.lib.power_system.keys())

        # 人物 → 势力
        for name, char in self.lib.characters.items():
            for rel_name in char.relationships.keys():
                if rel_name not in char_names and rel_name not in faction_names:
                    # 不一定是错误，关系可以指向外部
                    pass

        # 势力 → 成员
        for name, faction in self.lib.factions.items():
            for member in faction.key_members:
                if member not in char_names:
                    issues.append(f"[势力→人物] 势力「{name}」的成员「{member}」不在人物库中")

        # 地理 → 势力
        for name, geo in self.lib.geography.items():
            for faction_name in geo.related_factions:
                if faction_name not in faction_names:
                    issues.append(f"[地理→势力] 地理「{name}」关联的势力「{faction_name}」不在势力库中")

        # 历史 → 人物
        for name, hist in self.lib.history.items():
            for char_name in hist.related_characters:
                if char_name not in char_names:
                    issues.append(f"[历史→人物] 历史事件「{name}」关联的人物「{char_name}」不在人物库中")

        # 道具 → 人物（持有者）
        for name, item in self.lib.items.items():
            if item.owner and item.owner not in char_names:
                issues.append(f"[道具→人物] 道具「{name}」的持有者「{item.owner}」不在人物库中")

        return issues

    # ═══════════════════════════════════════════════════════
    # 概要生成（供 Agent prompt 使用）
    # ═══════════════════════════════════════════════════════

    def get_dead_character_names(self) -> list:
        """返回所有已死亡/退场角色名（供写前/写后一致性守卫使用）"""
        return [
            n for n, c in self.lib.characters.items()
            if is_dead(getattr(c, "current_status", "") or "")
        ]

    def get_summary(self, current_chapter: int = None, unresolved_hooks: list = None) -> str:
        """
        生成设定库概要文本，用于注入 prompt。

        分层策略（current_chapter 传入时启用）：
        - core：始终注入完整信息
        - supporting：仅当最近 20 章内活跃时注入摘要
        - minor：不注入 prompt（仅存于设定库供查阅）
        - ★ 伏笔保护：出现在未回收伏笔中的角色/势力/地点，无视窗口强制注入
        """
        ACTIVE_WINDOW = 20  # 活跃窗口：最近多少章

        # 从伏笔中提取被引用的条目名（防止遗忘）
        hook_names = set()
        if unresolved_hooks:
            for hook in unresolved_hooks:
                hook_str = str(hook)
                # 检查人物库中的名字是否出现在伏笔中
                for name in self.lib.characters:
                    if name in hook_str:
                        hook_names.add(f"characters:{name}")
                for name in self.lib.factions:
                    if name in hook_str:
                        hook_names.add(f"factions:{name}")
                for name in self.lib.geography:
                    if name in hook_str:
                        hook_names.add(f"geography:{name}")
                for name in self.lib.items:
                    if name in hook_str:
                        hook_names.add(f"items:{name}")

        parts = []

        # ── 战力设定（core 始终注入，supporting 按窗口） ──
        power_parts = []
        for name, ps in self.lib.power_system.items():
            imp = getattr(ps, "importance", "core")
            last = getattr(ps, "last_active_chapter", None)
            if imp == "core" or (imp == "supporting" and current_chapter
                                 and last and current_chapter - last <= ACTIVE_WINDOW):
                # LLM 返回的境界 dict 键名不固定（name/level/...），防御性取值
                levels_str = " → ".join(
                    (l.get("name") or l.get("level") or next(iter(l.values()), ""))
                    if isinstance(l, dict) else str(l)
                    for l in ps.levels[:10]
                )
                power_parts.append(f"{name}：{levels_str}。{ps.basic_info[:200]}")
        if power_parts:
            parts.append("【战力体系】" + "；".join(power_parts))

        # ── 人物（按分层筛选 + 伏笔保护 + 死亡守卫） ──
        if self.lib.characters:
            core_chars, active_chars, hook_protected_chars, dead_chars = [], [], [], []
            total = len(self.lib.characters)
            for name, c in self.lib.characters.items():
                imp = getattr(c, "importance", "supporting")
                last = getattr(c, "last_active_chapter", None)
                ability_str = f" 能力：{'/'.join(str(a) for a in c.abilities[:3])}" if c.abilities else ""
                status = (getattr(c, "current_status", "") or "").strip()
                is_hook_protected = f"characters:{name}" in hook_names

                # 死亡/退场角色单列警告区，不进入常规名单
                if is_dead(status):
                    dead_chars.append(f"{name}（{status[:40]}）")
                    continue

                status_str = f" 状态：{status[:30]}" if status else ""
                brief = f"{name}（{c.gender}{ability_str}{status_str}）"

                if imp == "core":
                    core_chars.append(brief)
                elif imp == "supporting":
                    # 伏笔保护 或 活跃窗口内 → 注入
                    if is_hook_protected:
                        hook_protected_chars.append(f"{brief} ⚡伏笔关联")
                    elif not current_chapter or (last and current_chapter - last <= ACTIVE_WINDOW):
                        active_chars.append(brief)
                elif imp == "minor":
                    # minor 通常不注入，但伏笔保护例外
                    if is_hook_protected:
                        hook_protected_chars.append(f"{brief} ⚡伏笔关联（minor）")

            # 死亡名单优先注入：下游 Agent 据此规避"死人复活"类剧情矛盾
            if dead_chars:
                parts.append(
                    "【⚠ 已死亡/退场人物 — 严禁在当前时间线安排其出场、说话、行动；"
                    "回忆/闪回/梦境/明确的复活剧情除外】\n"
                    + "\n".join(f"💀 {d}" for d in dead_chars)
                )

            char_lines = []
            if core_chars:
                char_lines.append(f"★核心（{len(core_chars)}人）：{'；'.join(core_chars)}")
            if active_chars:
                char_lines.append(f"◇活跃配角（{len(active_chars)}人）：{'；'.join(active_chars)}")
            if hook_protected_chars:
                char_lines.append(f"🔗伏笔关联（强制注入，{len(hook_protected_chars)}人）：{'；'.join(hook_protected_chars)}")
            filtered = total - len(core_chars) - len(active_chars) - len(hook_protected_chars) - len(dead_chars)
            if filtered > 0:
                char_lines.append(f"（另有 {filtered} 个非活跃/龙套角色未列出）")
            if char_lines:
                parts.append("【人物】" + "\n".join(char_lines))

        # ── 势力（按分层筛选 + 伏笔保护） ──
        if self.lib.factions:
            core_f, active_f, hook_f, total_f = [], [], [], len(self.lib.factions)
            for n, f in self.lib.factions.items():
                imp = getattr(f, "importance", "supporting")
                last = getattr(f, "last_active_chapter", None)
                is_hook_protected = f"factions:{n}" in hook_names
                entry = f"{n}（{f.type}）"
                if imp == "core":
                    core_f.append(entry)
                elif is_hook_protected:
                    hook_f.append(entry)
                elif imp == "supporting":
                    if not current_chapter or (last and current_chapter - last <= ACTIVE_WINDOW):
                        active_f.append(entry)
            fac_lines = []
            if core_f:
                fac_lines.append(f"★核心：{'；'.join(core_f)}")
            if active_f:
                fac_lines.append(f"◇活跃：{'；'.join(active_f)}")
            if hook_f:
                fac_lines.append(f"🔗伏笔关联（强制注入）：{'；'.join(hook_f)}")
            filtered_f = total_f - len(core_f) - len(active_f) - len(hook_f)
            if filtered_f > 0:
                fac_lines.append(f"（{filtered_f} 个非活跃势力未列出）")
            if fac_lines:
                parts.append("【势力】" + "\n".join(fac_lines))

        # ── 地理（按分层筛选 + 伏笔保护） ──
        if self.lib.geography:
            core_g, active_g, hook_g, total_g = [], [], [], len(self.lib.geography)
            for n, g in self.lib.geography.items():
                imp = getattr(g, "importance", "supporting")
                last = getattr(g, "last_active_chapter", None)
                is_hook_protected = f"geography:{n}" in hook_names
                if imp == "core":
                    core_g.append(n)
                elif is_hook_protected:
                    hook_g.append(n)
                elif imp == "supporting":
                    if not current_chapter or (last and current_chapter - last <= ACTIVE_WINDOW):
                        active_g.append(n)
            geo_lines = []
            if core_g:
                geo_lines.append(f"★核心：{'、'.join(core_g)}")
            if active_g:
                geo_lines.append(f"◇活跃：{'、'.join(active_g)}")
            if hook_g:
                geo_lines.append(f"🔗伏笔关联（强制注入）：{'、'.join(hook_g)}")
            filtered_g = total_g - len(core_g) - len(active_g) - len(hook_g)
            if filtered_g > 0:
                geo_lines.append(f"（{filtered_g} 处非活跃地点未列出）")
            if geo_lines:
                parts.append("【地理】" + "\n".join(geo_lines))

        # ── 道具（按分层筛选 + 伏笔保护） ──
        if self.lib.items:
            core_i, active_i, hook_i, total_i = [], [], [], len(self.lib.items)
            for n, it in self.lib.items.items():
                imp = getattr(it, "importance", "supporting")
                last = getattr(it, "last_active_chapter", None)
                is_hook_protected = f"items:{n}" in hook_names
                owner_str = f"（{it.owner}持有）" if it.owner else ""
                entry = f"{n}{owner_str}"
                if imp == "core":
                    core_i.append(entry)
                elif is_hook_protected:
                    hook_i.append(entry)
                elif imp == "supporting":
                    if not current_chapter or (last and current_chapter - last <= ACTIVE_WINDOW):
                        active_i.append(entry)
            item_lines = []
            if core_i:
                item_lines.append(f"★核心：{'；'.join(core_i)}")
            if active_i:
                item_lines.append(f"◇活跃：{'；'.join(active_i)}")
            if hook_i:
                item_lines.append(f"🔗伏笔关联（强制注入）：{'；'.join(hook_i)}")
            filtered_i = total_i - len(core_i) - len(active_i) - len(hook_i)
            if filtered_i > 0:
                item_lines.append(f"（{filtered_i} 件非活跃道具未列出）")
            if item_lines:
                parts.append("【道具】" + "\n".join(item_lines))

        # ── 历史（仅 core 和窗口内条目标注，minor 全部不列） ──
        if self.lib.history:
            hist_items = []
            skipped = 0
            for n, h in self.lib.history.items():
                imp = getattr(h, "importance", "supporting")
                last = getattr(h, "last_active_chapter", None)
                if imp == "core":
                    hist_items.append(f"★{n}（{h.time_period}）：{h.description[:100]}")
                elif imp == "supporting":
                    if not current_chapter or (last and current_chapter - last <= ACTIVE_WINDOW):
                        hist_items.append(f"{n}（{h.time_period}）：{h.description[:100]}")
                    else:
                        skipped += 1
                else:
                    skipped += 1
            if hist_items:
                parts.append("【历史】" + "；".join(hist_items))
            if skipped > 0:
                parts.append(f"（{skipped} 条非活跃历史未列出）")

        return "\n\n".join(parts) if parts else "（设定库为空）"


def create_empty_library() -> SettingLibrary:
    """创建空的设定库"""
    return SettingLibrary()
