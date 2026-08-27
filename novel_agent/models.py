"""
数据模型 — 所有核心数据结构
"""

from dataclasses import dataclass, field, asdict
from typing import Optional
import json


# ═══════════════════════════════════════════════════════════════
# 项目配置
# ═══════════════════════════════════════════════════════════════

@dataclass
class ProjectConfig:
    """项目全局配置 — 对应『勾框框』行为"""
    project_name: str = ""
    genre: str = "玄幻"                    # 故事分类
    narrative_person: str = "第三人称"      # 叙事人称
    writing_style: str = "番茄爆款"         # 文风
    internet_slang_level: str = "中"       # 网感程度
    core_idea: str = ""                    # 核心脑洞/思路
    core_setting: str = ""                 # 核心设定补充
    target_volumes: int = 5                # 目标卷数
    chapters_per_volume: int = 50          # 每卷章节数
    words_per_chapter: int = 3000          # 单章字数
    author_notes: str = ""                 # 作者额外备注


# ═══════════════════════════════════════════════════════════════
# 设定库 — 6 个子库（百度百科词条风格）
# ═══════════════════════════════════════════════════════════════

@dataclass
class CharacterEntry:
    """人物词条"""
    name: str                             # 姓名
    aliases: list = field(default_factory=list)        # 别名/称号
    gender: str = "男"
    age: str = ""
    appearance: str = ""                  # 外貌描述
    personality: str = ""                 # 性格
    background: str = ""                  # 背景故事
    abilities: list = field(default_factory=list)      # 能力/技能
    relationships: dict = field(default_factory=dict)  # 关系网 {人名: 关系描述}
    current_status: str = ""              # 当前状态（存活/所处位置/最新动态）
    first_appearance_chapter: Optional[int] = None
    importance: str = "supporting"        # core / supporting / minor — 控制 prompt 注入权重
    last_active_chapter: Optional[int] = None  # 最后活跃章节（用于活跃窗口筛选）
    notes: str = ""                       # 补充说明


@dataclass
class GeographyEntry:
    """地理词条"""
    name: str                             # 地名
    type: str = ""                        # 类型（城市/宗门/秘境/大陆…）
    description: str = ""                 # 详细描述
    significance: str = ""                # 在故事中的重要性
    related_factions: list = field(default_factory=list)  # 所属/关联势力
    related_characters: list = field(default_factory=list)  # 关联人物
    first_mentioned_chapter: Optional[int] = None
    importance: str = "supporting"        # core / supporting / minor
    last_active_chapter: Optional[int] = None
    notes: str = ""


@dataclass
class HistoryEntry:
    """历史事件词条"""
    name: str                             # 事件名
    time_period: str = ""                 # 发生时期（如"三千年前""上古时代"）
    description: str = ""                 # 事件描述
    impact: str = ""                      # 对当前世界的影响
    related_characters: list = field(default_factory=list)
    related_factions: list = field(default_factory=list)
    revealed_in_chapter: Optional[int] = None
    importance: str = "supporting"
    last_active_chapter: Optional[int] = None
    notes: str = ""


@dataclass
class PowerSystemEntry:
    """战力设定词条"""
    name: str                             # 体系名（如"武魂体系""灵气修炼体系"）
    category: str = ""                    # 分类（修炼/魔法/武魂/科技/…）
    levels: list = field(default_factory=list)  # 境界列表 [{"name":"练气期","description":"…"}, …]
    basic_info: str = ""                  # 基础设定（前5章必须交代）
    advanced_info: str = ""               # 高级设定（后续逐步探索）
    special_cases: str = ""               # 特殊情况（如双生武魂、变异体质等）
    first_explained_chapter: Optional[int] = None
    importance: str = "core"             # 战力体系默认为 core
    last_active_chapter: Optional[int] = None
    notes: str = ""


@dataclass
class FactionEntry:
    """势力词条"""
    name: str                             # 势力名
    type: str = ""                        # 类型（宗门/帝国/家族/组织/散修联盟…）
    description: str = ""                 # 详细描述
    leader: str = ""                      # 首领
    key_members: list = field(default_factory=list)  # 核心成员
    territory: str = ""                   # 势力范围/领地
    relationships: dict = field(default_factory=dict)  # 与其他势力的关系
    first_mentioned_chapter: Optional[int] = None
    importance: str = "supporting"        # core / supporting / minor
    last_active_chapter: Optional[int] = None
    notes: str = ""


@dataclass
class ItemEntry:
    """道具词条（武器/丹药/信物/功法/秘宝…）"""
    name: str                             # 道具名
    type: str = ""                        # 类型（武器/丹药/信物/功法/秘宝…）
    description: str = ""                 # 详细描述
    owner: str = ""                       # 当前持有者（人名，须在人物库中存在）
    current_status: str = ""              # 当前状态（在谁手中/已损毁/已服用…）
    first_mentioned_chapter: Optional[int] = None
    importance: str = "supporting"        # core / supporting / minor
    last_active_chapter: Optional[int] = None
    notes: str = ""


@dataclass
class SettingLibrary:
    """设定库 — 包含全部 6 个子库"""
    characters: dict = field(default_factory=dict)     # {name: CharacterEntry}
    geography: dict = field(default_factory=dict)       # {name: GeographyEntry}
    history: dict = field(default_factory=dict)         # {name: HistoryEntry}
    power_system: dict = field(default_factory=dict)    # {name: PowerSystemEntry}
    factions: dict = field(default_factory=dict)        # {name: FactionEntry}
    items: dict = field(default_factory=dict)           # {name: ItemEntry}


# ═══════════════════════════════════════════════════════════════
# 粗纲 & 细纲
# ═══════════════════════════════════════════════════════════════

@dataclass
class VolumeOutline:
    """单卷粗纲"""
    volume_number: int = 1
    volume_title: str = ""                # 卷标题
    chapter_range: str = ""               # 如"第1-50章"
    narrative_outline: str = ""           # ★ 核心：叙事化行文脉络
    main_conflicts: list = field(default_factory=list)       # 主要冲突
    character_arcs: dict = field(default_factory=dict)       # {角色名: 本卷成长弧线描述}
    foreshadowing_planted: list = field(default_factory=list)  # 本卷设下的伏笔
    foreshadowing_recovered: list = field(default_factory=list)  # 本卷回收的伏笔
    key_events: list = field(default_factory=list)            # 关键事件节点
    volume_ending_hook: str = ""          # 卷末钩子
    background_release_plan: str = ""     # 前5章背景释放计划（仅第一卷必有）
    pacing_plan: str = ""                 # 张弛节奏表：哪些段落是爆发高潮、哪些是缓冲休整
    author_notes: str = ""                # 作者备注/修改意见


@dataclass
class ChapterOutline:
    """单章细纲（任务式）"""
    chapter_number: int = 1
    chapter_title: str = ""
    volume_reference: str = ""            # 呼应粗纲的哪部分
    chapter_objective: str = ""           # 本章目标（一句话）
    scenes: list = field(default_factory=list)  # 场景列表 [{"location":"","summary":"","purpose":""}]

    # ★ 核心：任务清单
    character_updates: list = field(default_factory=list)      # 主角/角色信息更新
    foreshadowing_plant: list = field(default_factory=list)    # 伏笔设下
    foreshadowing_recover: list = field(default_factory=list)  # 伏笔回收
    hooks_set: list = field(default_factory=list)              # 结尾钩子
    world_building_revealed: list = field(default_factory=list)  # 世界观信息释放
    conflicts_advanced: list = field(default_factory=list)     # 冲突推进

    characters_appearing: list = field(default_factory=list)   # 出场人物
    locations: list = field(default_factory=list)              # 出场地点
    pacing_type: str = ""                 # 本章节奏类型：爆发 / 推进 / 缓冲
    writing_notes: str = ""               # 写作注意事项
    author_notes: str = ""                # 作者备注


@dataclass
class ChapterSummary:
    """章节摘要（用于上下文管理）"""
    chapter_number: int = 0
    title: str = ""
    summary: str = ""                     # 3-5 句话摘要
    new_characters: list = field(default_factory=list)
    new_locations: list = field(default_factory=list)
    key_events: list = field(default_factory=list)
    unresolved_hooks: list = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# 项目完整状态
# ═══════════════════════════════════════════════════════════════

@dataclass
class ProjectState:
    """项目的完整可序列化状态"""
    config: ProjectConfig = field(default_factory=ProjectConfig)
    setting_library: SettingLibrary = field(default_factory=SettingLibrary)
    volume_outlines: dict = field(default_factory=dict)       # {volume_number: VolumeOutline}
    chapter_outlines: dict = field(default_factory=dict)      # {chapter_number: ChapterOutline}
    chapter_summaries: dict = field(default_factory=dict)     # {chapter_number: ChapterSummary}
    chapter_contents: dict = field(default_factory=dict)     # {chapter_number: ChapterContent}
    current_volume: int = 1
    current_chapter: int = 1
    unresolved_hooks_global: list = field(default_factory=list)  # 全局未回收伏笔


# ═══════════════════════════════════════════════════════════════
# 伏笔台账
# ═══════════════════════════════════════════════════════════════

@dataclass
class Foreshadow:
    """单条伏笔（全局台账，按 ID 追踪，替代自由文本列表）"""
    id: str                                 # 如 "F-001"
    text: str                               # 伏笔描述
    planted_chapter: int = 0                # 设下章节
    status: str = "open"                    # open / recovered / archived
    recovered_chapter: Optional[int] = None # 回收章节
    last_mention_chapter: Optional[int] = None  # 最近一次被提及/推进的章节
    mentions: int = 0                       # 被提及次数
    sticky: bool = False                    # 卷级/跨卷主线伏笔：禁模糊回收，仅允许显式 ID 回收


# ═══════════════════════════════════════════════════════════════
# 章节正文 & 审查结果
# ═══════════════════════════════════════════════════════════════

@dataclass
class ChapterContent:
    """章节正文"""
    chapter_number: int = 0
    title: str = ""
    content: str = ""                     # 正文内容
    word_count: int = 0                   # 实际字数
    created_at: str = ""                  # 生成时间
    revision_count: int = 0               # 修改次数
    author_notes: str = ""                # 作者备注


@dataclass
class ReviewResult:
    """审查结果（小纲审查 / 内容校验 通用）"""
    passed: bool = False                  # 是否通过
    score: int = 0                        # 0-100 评分
    issues: list = field(default_factory=list)       # 问题列表 [{"severity":"error/warning","category":"","description":""}]
    suggestions: list = field(default_factory=list)  # 修改建议
    detail: str = ""                      # 详细审查报告
