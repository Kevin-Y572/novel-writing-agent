#!/usr/bin/env python3
"""
主入口 — 交互式 CLI 工作流
Phase 0: 项目初始化（核心脑洞 + 勾框框）
Phase 1: 设定库初始化（5 库 AI 生成 + 人工审核）
Phase 2: 第一卷粗纲生成（叙事化 + 前5章背景释放）
Phase 3: 逐章细纲生成（任务式 checklist）
Phase 4: 后续卷循环
"""

import sys
import os
import json

# 确保可以 import 同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    GENRES, NARRATIVE_PERSONS, STYLE_PRESETS, INTERNET_SLANG_LEVELS,
)
from models import ProjectConfig, ProjectState, ChapterSummary, VolumeOutline
from api_client import get_client, APIClient
from project_manager import ProjectManager
from setting_library import SettingLibraryManager
from rough_outline_agent import RoughOutlineAgent
from detailed_outline_agent import DetailedOutlineAgent
from outline_review_agent import OutlineReviewAgent
from chapter_writing_agent import ChapterWritingAgent
from content_review_agent import ContentReviewAgent
from setting_maintenance_agent import SettingMaintenanceAgent
from prompts import (
    SETTING_UPDATE_SYSTEM, get_setting_update_user,
)
from utils import (
    print_header, print_subheader, print_section,
    print_success, print_warning, print_error, print_info, print_list,
    ask, ask_yes_no, ask_choice, ask_multiline, press_enter_to_continue,
    SEPARATOR, SEPARATOR_THIN,
)


# ═══════════════════════════════════════════════════════════════
# 主应用类
# ═══════════════════════════════════════════════════════════════

class NovelAgentApp:
    """网文创作 Agent 主应用"""

    def __init__(self):
        self.pm: ProjectManager | None = None
        self.state: ProjectState | None = None
        self.slm: SettingLibraryManager | None = None

    def run(self):
        """主入口"""
        self._show_welcome()

        # 检查 API 配置
        if not self._check_api():
            return

        # 选择：新建项目 / 加载续写 / 书架管理
        action = self._main_menu()

        if action == "new":
            self._new_book_wizard()

        elif action == "load":
            self._load_and_continue()

        elif action == "shelf":
            self._open_bookshelf()

    # ═══════════════════════════════════════════════════════
    # 欢迎 & 菜单
    # ═══════════════════════════════════════════════════════

    def _show_welcome(self):
        print_header("🔮 多 AI Agent 网络小说智能创作系统 — Demo")
        print("  粗纲 & 细纲生成模块")
        print(f"  模型：DeepSeek V4 Pro")
        print(SEPARATOR_THIN)

    def _check_api(self) -> bool:
        """检查 API Key 是否配置"""
        from config import DEEPSEEK_API_KEY
        if DEEPSEEK_API_KEY == "your-api-key-here":
            print_warning("DeepSeek API Key 未配置！")
            print("\n  请在 config.py 中设置 DEEPSEEK_API_KEY")
            print("  或设置环境变量：export DEEPSEEK_API_KEY=sk-xxx")

            key = input("\n  你也可以现在输入 API Key（直接回车跳过）: ").strip()
            if key:
                import config
                import api_client as ac
                config.DEEPSEEK_API_KEY = key
                ac._client_instance = None  # 强制重建 client
                print_success("API Key 已临时设置")
                return True
            else:
                print_warning("未设置 API Key，退出")
                return False
        return True

    def _main_menu(self) -> str:
        """主菜单"""
        print_header("主菜单")
        print("  [1] 创建新项目")
        print("  [2] 加载已有项目（快速续写）")
        print("  [3] 书架管理（多本书籍 · 记忆 · 维护）")
        print("  [0] 退出")

        while True:
            choice = input("\n  请选择: ").strip()
            if choice == "1":
                return "new"
            elif choice == "2":
                return "load"
            elif choice == "3":
                return "shelf"
            elif choice == "0":
                print("  再见！")
                sys.exit(0)

    # ═══════════════════════════════════════════════════════
    # 书架集成
    # ═══════════════════════════════════════════════════════

    def _new_book_wizard(self):
        """新建书籍完整向导(Phase 0-3)"""
        self._phase_0_init_project()
        self._phase_1_init_settings()
        self._phase_2_volume_outline()
        self._phase_3_chapter_loop()

    def _open_bookshelf(self):
        """打开交互式书架"""
        from bookshelf import BookshelfManager, BookshelfUI
        manager = BookshelfManager()
        manager.scan()
        BookshelfUI(
            manager,
            on_continue_manager=self._continue_manager_book,
            on_new_book=self._new_book_wizard,
        ).run()

    def _continue_manager_book(self, book):
        """书架回调:续写交互式格式(ProjectManager)的书"""
        self.pm = ProjectManager(book.title)
        self.state = self.pm.load(book.title)
        if self.state is None:
            print_error("项目数据加载失败")
            return
        self.continue_project()

    def continue_project(self):
        """已加载项目的续写入口:补粗纲(如缺)→ 章节循环"""
        self.slm = SettingLibraryManager(self.state.setting_library, self.state.config)

        # 判断当前进度，跳转到对应阶段
        vol_key = str(self.state.current_volume)
        if vol_key not in self.state.volume_outlines:
            print("\n  当前无粗纲，进入粗纲生成阶段")
            self._phase_2_volume_outline()

        self._phase_3_chapter_loop()

    # ═══════════════════════════════════════════════════════
    # Phase 0: 项目初始化
    # ═══════════════════════════════════════════════════════

    def _phase_0_init_project(self):
        """Phase 0: 用户输入核心脑洞 + 勾框框"""
        print_header("Phase 0 · 项目初始化")
        print("  请提供你的创作方向和偏好设置\n")

        # 核心脑洞
        core_idea = ask_multiline(
            "请输入你的核心脑洞/创意（多行输入，空行结束）："
        )
        if not core_idea:
            core_idea = ask("或简单一句话描述你的故事想法")

        # 勾框框
        genre = ask_choice("故事分类", GENRES, default_idx=0)
        narrative_person = ask_choice("叙事人称", NARRATIVE_PERSONS, default_idx=1)
        writing_style = ask_choice(
            "文风", list(STYLE_PRESETS.keys()), default_idx=0
        )
        internet_slang = ask_choice(
            "网络热梗与网感程度", list(INTERNET_SLANG_LEVELS.keys()), default_idx=2
        )

        # 项目名称
        default_name = (
            core_idea[:20].replace("\n", " ").strip()
            if core_idea else "未命名项目"
        )
        project_name = ask("项目名称", default_name)

        # AI 自动书名（用户未指定时）
        if not project_name or project_name == default_name or project_name == "未命名项目":
            if ask_yes_no("是否让 AI 根据脑洞自动取书名？", default="y"):
                print("  🤖 AI 正在构思书名...")
                ai_name = self._generate_book_title(core_idea, genre)
                if ai_name:
                    print(f"  📖 AI 建议书名：《{ai_name}》")
                    if ask_yes_no("是否采用？", default="y"):
                        project_name = ai_name
                    else:
                        project_name = ask("请输入你决定的书名", ai_name)

        # 补充设定
        print()
        core_setting = ask("核心设定补充（没有可直接回车）", "")

        # 章节配置
        print()
        chapters_per_vol = ask("每卷章节数", "50")
        words_per_chap = ask("每章目标字数", "3000")
        author_notes = ask("其他备注（没有可直接回车）", "")

        config = ProjectConfig(
            project_name=project_name,
            genre=genre,
            narrative_person=narrative_person,
            writing_style=writing_style,
            internet_slang_level=internet_slang,
            core_idea=core_idea,
            core_setting=core_setting,
            chapters_per_volume=int(chapters_per_vol) if chapters_per_vol.isdigit() else 50,
            words_per_chapter=int(words_per_chap) if words_per_chap.isdigit() else 3000,
            author_notes=author_notes,
        )

        # 创建项目
        self.pm = ProjectManager(project_name)
        self.state = self.pm.create_new(config)
        self.slm = SettingLibraryManager(self.state.setting_library, config)

        # 登记到书架并设为当前书
        try:
            from bookshelf import BookshelfManager
            BookshelfManager().mark_opened(os.path.basename(self.pm.project_dir.rstrip("/\\")))
        except Exception:
            pass  # 书架登记失败不影响创作流程

        print_success("项目初始化完成！")
        press_enter_to_continue()

    # ═══════════════════════════════════════════════════════
    # Phase 1: 设定库初始化
    # ═══════════════════════════════════════════════════════

    def _phase_1_init_settings(self):
        """Phase 1: AI 生成初始设定库 + 人工审核"""
        print_header("Phase 1 · 设定库初始化")

        if not ask_yes_no("是否使用 AI 自动生成初始设定库？否则手动创建空库", default="y"):
            print("  已创建空设定库，可后续手动添加条目")
            press_enter_to_continue()
            return

        # AI 生成
        result = self.slm.generate_initial_settings()

        if not result or "_parse_error" in result:
            print_warning("AI 生成遇到问题，进入手动审核模式")
        else:
            total = sum(
                len(self.state.setting_library.__dict__[k])
                for k in self.slm.get_library_names()
            )
            print_success(f"初始设定库已生成（共约 {total} 条）")

        # 展示 & 审核
        self._setting_review_loop()

        # 一致性检查
        issues = self.slm.check_consistency()
        if issues:
            print_warning(f"发现 {len(issues)} 个潜在矛盾：")
            for issue in issues:
                print(f"    · {issue}")
        else:
            print_success("设定库一致性检查通过，未发现矛盾")

        # 保存
        self.pm.save_setting_library()
        print_success("设定库已保存")
        press_enter_to_continue()

    def _setting_review_loop(self):
        """设定库审核循环"""
        lib_names = self.slm.get_library_names()

        while True:
            print_header("设定库管理")
            for name in lib_names:
                count = self.slm.get_entry_count(name)
                label = self.slm.get_library_label(name)
                print(f"  [{lib_names.index(name)+1}] {label}（{count} 条）")
            print(f"  [V] 查看所有条目详情")
            print(f"  [A] 手动添加条目")
            print(f"  [D] 删除条目")
            print(f"  [C] 一致性检查")
            print(f"  [Q] 完成审核，进入下一步")

            choice = input("\n  请选择: ").strip().upper()

            if choice == "Q":
                break
            elif choice == "V":
                self._view_all_entries()
            elif choice == "A":
                self._add_entry_manually()
            elif choice == "D":
                self._delete_entry()
            elif choice == "C":
                issues = self.slm.check_consistency()
                if issues:
                    print_warning(f"发现 {len(issues)} 个潜在矛盾：")
                    for i in issues:
                        print(f"    · {i}")
                else:
                    print_success("未发现矛盾")
            elif choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(lib_names):
                    self._view_library(lib_names[idx])
            press_enter_to_continue()

    def _view_library(self, lib_name: str):
        """查看某个子库的所有条目"""
        print(self.slm.display_all_entries(lib_name))
        entries = self.slm.get_entries(lib_name)
        if entries and ask_yes_no("是否查看某条目详情？", default="n"):
            entry_name = ask("输入条目名称")
            if entry_name in entries:
                print(self.slm.display_entry(lib_name, entry_name))

    def _view_all_entries(self):
        """查看所有子库"""
        for name in self.slm.get_library_names():
            print(self.slm.display_all_entries(name))
            print()

    def _add_entry_manually(self):
        """手动添加条目"""
        lib_names = self.slm.get_library_names()
        lib_labels = [self.slm.get_library_label(n) for n in lib_names]
        selected_label = ask_choice("选择要添加到的子库", lib_labels)
        lib_name = lib_names[lib_labels.index(selected_label)]

        entry_name = ask("条目名称")
        if not entry_name:
            return

        print("  请输入以下信息（直接回车跳过）：")
        description = ask("描述", "")

        if lib_name == "characters":
            self.slm.add_entry(lib_name, entry_name,
                               gender=ask("性别", "男"),
                               age=ask("年龄", ""),
                               appearance=ask("外貌", ""),
                               personality=ask("性格", ""),
                               background=ask("背景", description),
                               abilities=ask("能力（逗号分隔）", "").split(",") if ask("能力（逗号分隔）", "") else [],
                               )
        elif lib_name == "power_system":
            self.slm.add_entry(lib_name, entry_name,
                               category=ask("分类", "修炼体系"),
                               basic_info=ask("基础设定", description),
                               advanced_info=ask("高级设定", ""),
                               )
        elif lib_name == "factions":
            self.slm.add_entry(lib_name, entry_name,
                               type=ask("类型", ""),
                               description=description,
                               leader=ask("首领", ""),
                               territory=ask("势力范围", ""),
                               )
        elif lib_name == "geography":
            self.slm.add_entry(lib_name, entry_name,
                               type=ask("类型", ""),
                               description=description,
                               significance=ask("重要性", ""),
                               )
        elif lib_name == "history":
            self.slm.add_entry(lib_name, entry_name,
                               time_period=ask("时期", ""),
                               description=description,
                               impact=ask("影响", ""),
                               )

        print_success(f"条目「{entry_name}」已添加")
        self.pm.save_setting_library()

    def _delete_entry(self):
        """删除条目"""
        lib_names = self.slm.get_library_names()
        lib_labels = [self.slm.get_library_label(n) for n in lib_names]
        selected_label = ask_choice("选择子库", lib_labels)
        lib_name = lib_names[lib_labels.index(selected_label)]

        entries = self.slm.get_entries(lib_name)
        if not entries:
            print_warning("该库暂无条目")
            return
        print(self.slm.display_all_entries(lib_name))
        entry_name = ask("要删除的条目名称")
        if entry_name in entries and ask_yes_no(f"确认删除「{entry_name}」？", default="n"):
            self.slm.remove_entry(lib_name, entry_name)
            print_success(f"已删除「{entry_name}」")
            self.pm.save_setting_library()

    # ═══════════════════════════════════════════════════════
    # Phase 2: 第一卷粗纲生成
    # ═══════════════════════════════════════════════════════

    def _phase_2_volume_outline(self):
        """Phase 2: 第一卷粗纲"""
        print_header("Phase 2 · 粗纲生成")

        agent = RoughOutlineAgent(self.state.config, self.state.setting_library)
        setting_summary = self.slm.get_summary(current_chapter=1)

        outline = agent.interactive_outline_loop(
            volume_number=1,
            setting_summary=setting_summary,
            previous_summary="",
            unresolved_hooks="",
        )

        if outline is None:
            print_warning("粗纲生成被跳过")
            return

        # 保存
        self.state.volume_outlines["1"] = outline.__dict__
        self.state.current_volume = 1
        self.pm.save_volume_outline(1)
        self.pm.save_meta()

        # 提取全局伏笔（入台账，兼容旧的 unresolved_hooks_global 字段）
        from foreshadow_registry import ForeshadowRegistry
        registry = self.pm.load_registry()
        registry.register_many(outline.foreshadowing_planted, chapter=0, sticky=True)
        self.state.unresolved_hooks_global = registry.open_texts()
        self.pm.save_registry(registry)

        print_success("第一卷粗纲已保存！")
        press_enter_to_continue()

    # ═══════════════════════════════════════════════════════
    # Phase 3: 逐章细纲循环
    # ═══════════════════════════════════════════════════════

    def _phase_3_chapter_loop(self):
        """Phase 3: 逐章细纲生成循环"""
        print_header("Phase 3 · 逐章细纲生成")

        volume_outline_data = self.state.volume_outlines.get("1", {})
        volume_outline = VolumeOutline(**volume_outline_data) \
            if volume_outline_data else None

        if volume_outline is None:
            print_warning("未找到卷粗纲，请先生成粗纲")
            return

        agent = DetailedOutlineAgent(self.state.config, self.state.setting_library)

        start_chapter = self.state.current_chapter
        total_chapters = self.state.config.chapters_per_volume

        print(f"  当前进度：第 {start_chapter} 章 / 共 {total_chapters} 章")
        print(f"  输入 'q' 可随时退出，输入 's' 可跳过某章\n")

        chapter = start_chapter
        while chapter <= total_chapters:
            print(SEPARATOR)
            print(f"  📖 第 {chapter} 章 / {total_chapters}")
            print(SEPARATOR)

            # 检查是否要跳过
            action = input(f"  [生成(g) / 跳过(s) / 退出(q)]: ").strip().lower()
            if action == "q":
                break
            elif action == "s":
                chapter += 1
                continue

            # 构建上下文（按活跃窗口筛选设定库）
            # 活跃窗口筛选 + 伏笔保护
            unresolved = self._build_unresolved_hooks()
            setting_summary = self.slm.get_summary(
                current_chapter=chapter,
                unresolved_hooks=self.state.unresolved_hooks_global,
            )
            prev_summary = self._build_previous_summary(chapter)

            # 生成细纲
            outline = agent.interactive_outline_loop(
                chapter_number=chapter,
                volume_outline=volume_outline,
                setting_summary=setting_summary,
                previous_chapters_summary=prev_summary,
                unresolved_hooks=unresolved,
            )

            if outline is None:
                if ask_yes_no("是否跳过本章？", default="y"):
                    chapter += 1
                continue

            # 保存细纲
            self.state.chapter_outlines[str(chapter)] = outline.__dict__
            self.pm.save_chapter_outline(chapter)

            # 更新全局伏笔
            self._update_global_hooks(outline)

            # 生成章节摘要
            self._generate_chapter_summary(outline, chapter)

            print_success(f"第 {chapter} 章细纲已保存！")

            # ═══════════════════════════════════════════════
            # ★ 新增：审查 → 写作 → 校验 → 维护 流水线
            # ═══════════════════════════════════════════════

            # Step A: 小纲审查
            self._run_outline_review(outline, chapter)

            # Step B: 章节写作
            chapter_content = self._run_chapter_writing(outline, chapter)
            if chapter_content is None:
                chapter += 1
                continue

            # Step C: 文章校验
            review_passed = self._run_content_review(chapter_content, outline, chapter)
            if not review_passed:
                if not ask_yes_no("校验未通过，是否仍保存本章？", default="n"):
                    if ask_yes_no("是否重新写作？", default="y"):
                        # 重新写作（简化：跳过审查，直接重新写作）
                        chapter_content = self._run_chapter_writing(outline, chapter)
                        if chapter_content is None:
                            chapter += 1
                            continue
                        self._run_content_review(chapter_content, outline, chapter)

            # 保存章节正文
            self.state.chapter_contents[str(chapter)] = chapter_content.__dict__
            self.pm.save_chapter_content(chapter, chapter_content)

            # Step D: 数据库维护
            self._run_setting_maintenance(chapter_content, outline, chapter)

            # 更新进度
            self.state.current_chapter = chapter + 1
            self.pm.save_meta()

            print_success(f"第 {chapter} 章完成！")
            chapter += 1

        print_header("本卷细纲生成完毕")
        print(f"  已完成：{chapter - start_chapter} 章")
        press_enter_to_continue()

    # ═══════════════════════════════════════════════════════
    # 辅助方法
    # ═══════════════════════════════════════════════════════

    def _generate_book_title(self, core_idea: str, genre: str) -> str:
        """AI 根据脑洞自动生成书名"""
        client = get_client()
        prompt = f"""请根据以下小说创作信息，生成 3 个吸引人的网络小说书名。

【故事分类】{genre}
【核心脑洞】{core_idea}

网文书名有多种成功风格，请各取一个：
1. **短书名**（2-5字）：简洁霸气，如《斗破苍穹》《遮天》《诡秘之主》
2. **中书名**（5-10字）：带修饰或标签，如《大奉打更人》《一念永恒》《剑来》
3. **长书名/轻小说风**（10-25字）：直接交代核心设定和卖点，如《我在精神病院学斩神》《关于我转生变成史莱姆这档事》《重生之都市修仙》《这个勇者明明超强却过分慎重》

要求：
- 每种风格的标题都能让读者一眼看出故事核心卖点
- 长书名要像一句完整的卖点宣言，读者读完书名就知道这书讲什么
- 新手作者更偏好长书名（降低理解门槛，直接传达设定）

请用以下格式输出（每行一个，不要编号）："""

        try:
            raw = client.chat(
                system_prompt="你是网络小说书名创意专家。输出格式：每行一个书名，不加编号、引号或解释。",
                user_prompt=prompt,
                temperature=0.9,
                max_tokens=120,
            )
            # 解析：每行一个书名
            titles = []
            for line in raw.strip().split("\n"):
                title = line.strip().strip('《》""\'\'。， -•·1234567890.')
                # 去掉可能的编号前缀
                if title and len(title) >= 2:
                    # 去掉 "1. " "1）" "1、" 等编号
                    for prefix in ["1.", "2.", "3.", "1）", "2）", "3）", "1、", "2、", "3、",
                                   "1 ", "2 ", "3 ", "·", "- "]:
                        if title.startswith(prefix):
                            title = title[len(prefix):].strip()
                            break
                    if title:
                        titles.append(title)

            if not titles:
                return ""

            # 让用户选一个
            print()
            for i, t in enumerate(titles[:3], 1):
                print(f"    [{i}] 《{t}》")
            choice = input("  选择一个，或直接回车选第一个: ").strip()
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(titles):
                    return titles[idx]
            except ValueError:
                pass
            return titles[0]
        except Exception:
            return ""

    def _build_previous_summary(self, current_chapter: int) -> str:
        """构建前文摘要"""
        if current_chapter == 1:
            return "（这是第一章，无前文）"

        summaries = []
        for ch in range(1, current_chapter):
            key = str(ch)
            if key in self.state.chapter_summaries:
                cs = self.state.chapter_summaries[key]
                if isinstance(cs, dict):
                    title = cs.get("title", f"第{ch}章")
                    summary = cs.get("summary", "")
                else:
                    title = getattr(cs, "title", f"第{ch}章")
                    summary = getattr(cs, "summary", "")
                summaries.append(f"第{ch}章「{title}」：{summary}")

        return "\n".join(summaries[-10:]) if summaries else "（无前文摘要）"

    def _build_unresolved_hooks(self) -> str:
        """构建未回收伏笔列表（优先走台账）"""
        registry = self.pm.load_registry()
        text = registry.hooks_text(self.state.current_chapter)
        if text != "（暂无）":
            return "以下伏笔尚未回收：\n" + "\n".join(f"  · {line[2:]}" for line in text.split("\n"))
        hooks = self.state.unresolved_hooks_global
        if not hooks:
            return "（暂无）"
        return "以下伏笔尚未回收：\n" + "\n".join(f"  · {h}" for h in hooks)

    def _update_global_hooks(self, outline):
        """更新全局伏笔追踪（台账 ID 化，替代子串匹配去重）"""
        from foreshadow_registry import ForeshadowRegistry
        registry = self.pm.load_registry()
        chapter = outline.chapter_number or self.state.current_chapter
        registry.register_many(outline.foreshadowing_plant, chapter)
        for r in (outline.foreshadowing_recover or []):
            registry.recover(r, chapter)
        self.pm.save_registry(registry)

    def _update_settings_from_chapter(self, outline, chapter_number: int):
        """
        AI 智能更新设定库：
        1. 从细纲中提取新人物/地点/战力/势力/历史事件 → 以百度百科风格入库
        2. 更新已有条目的最新状态（如角色战力提升、势力变更）
        3. 检查与现有设定库的矛盾
        """
        print("\n  🔍 AI 正在从本章细纲中提炼设定更新...")

        # 序列化细纲
        outline_json = json.dumps(outline.__dict__, ensure_ascii=False, indent=2, default=str)
        current_summary = self.slm.get_summary(
            current_chapter=chapter_number,
            unresolved_hooks=self.state.unresolved_hooks_global,
        )

        client = get_client()
        user_prompt = get_setting_update_user(
            chapter_number=chapter_number,
            chapter_outline_json=outline_json,
            current_settings_summary=current_summary,
        )

        try:
            result = client.chat_with_json_output(
                SETTING_UPDATE_SYSTEM, user_prompt,
                temperature=0.4, max_tokens=8192,
            )
        except Exception as e:
            print_warning(f"设定更新 API 调用失败: {e}，回退到简单入库")
            self._simple_auto_add(outline, chapter_number)
            return

        if "_parse_error" in result:
            print_warning("设定更新 JSON 解析失败，回退到简单入库")
            self._simple_auto_add(outline, chapter_number)
            return

        # ── 处理新条目 ──────────────────────────────────
        new_entries = result.get("new_entries", {})
        entry_added = 0
        for lib_name in ["characters", "geography", "history", "power_system", "factions"]:
            entries = new_entries.get(lib_name, {})
            for entry_name, entry_data in entries.items():
                if not entry_data or not isinstance(entry_data, dict):
                    continue
                # 避免覆盖已有条目
                if self.slm.entry_exists(lib_name, entry_name):
                    continue
                try:
                    # 自动添加首次出场章节标记
                    if lib_name == "characters":
                        entry_data.setdefault("first_appearance_chapter", chapter_number)
                    elif lib_name == "geography" or lib_name == "factions":
                        entry_data.setdefault("first_mentioned_chapter", chapter_number)
                    elif lib_name == "history":
                        entry_data.setdefault("revealed_in_chapter", chapter_number)
                    elif lib_name == "power_system":
                        entry_data.setdefault("first_explained_chapter", chapter_number)
                    self.slm.add_entry(lib_name, entry_name, **entry_data)
                    entry_added += 1
                except Exception as e:
                    print_warning(f"添加条目「{entry_name}」失败: {e}")

        # ── 处理已有条目更新 ────────────────────────────
        updates = result.get("updates", {})
        update_count = 0
        for lib_name in ["characters", "factions", "geography", "history", "power_system"]:
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

        # ── 自动刷新活跃窗口：本章出场/提及的条目，last_active_chapter 更新 ──
        active_bump = 0
        for name in outline.characters_appearing:
            if name and self.slm.entry_exists("characters", name):
                self.slm.update_entry("characters", name, last_active_chapter=chapter_number)
                active_bump += 1
        for name in outline.locations:
            if name and self.slm.entry_exists("geography", name):
                self.slm.update_entry("geography", name, last_active_chapter=chapter_number)
                active_bump += 1

        # ── 显示结果 ────────────────────────────────────
        if entry_added > 0:
            print_success(f"新增 {entry_added} 个设定条目")
        if update_count > 0:
            print_success(f"更新 {update_count} 个已有条目")
        if active_bump > 0:
            print(f"  🔄 刷新 {active_bump} 个条目的活跃时间戳")

        # ── 显示矛盾 ────────────────────────────────────
        issues = result.get("consistency_issues", [])
        if issues:
            print_warning(f"AI 发现 {len(issues)} 个潜在矛盾：")
            for issue in issues:
                print(f"    · {issue}")

        # ── 额外做一次本地一致性检查 ─────────────────────
        local_issues = self.slm.check_consistency()
        if local_issues:
            print_warning(f"本地一致性检查发现 {len(local_issues)} 个问题：")
            for issue in local_issues[:5]:  # 最多显示 5 条
                print(f"    · {issue}")

        self.pm.save_setting_library()

    def _simple_auto_add(self, outline, chapter_number: int):
        """简单自动入库（AI 失败时的回退方案）"""
        from models import CharacterEntry, GeographyEntry
        added = 0
        for name in outline.characters_appearing:
            if name and name not in self.state.setting_library.characters:
                self.state.setting_library.characters[name] = CharacterEntry(
                    name=name, first_appearance_chapter=chapter_number,
                    notes="（待 AI 完善）",
                )
                added += 1
        for name in outline.locations:
            if name and name not in self.state.setting_library.geography:
                self.state.setting_library.geography[name] = GeographyEntry(
                    name=name, first_mentioned_chapter=chapter_number,
                    notes="（待 AI 完善）",
                )
                added += 1
        if added > 0:
            print_success(f"简单入库 {added} 个条目（待后续 AI 完善）")
        self.pm.save_setting_library()

    def _generate_chapter_summary(self, outline, chapter_number: int):
        """生成章节摘要"""
        tasks = []
        if outline.character_updates:
            tasks.append(f"角色：{'；'.join(outline.character_updates[:3])}")
        if outline.conflicts_advanced:
            tasks.append(f"冲突：{'；'.join(outline.conflicts_advanced[:2])}")
        if outline.foreshadowing_plant:
            tasks.append(f"伏笔：{'；'.join(outline.foreshadowing_plant[:2])}")

        summary_text = f"{outline.chapter_objective}。" + "。".join(tasks) if tasks else outline.chapter_objective

        self.state.chapter_summaries[str(chapter_number)] = ChapterSummary(
            chapter_number=chapter_number,
            title=outline.chapter_title,
            summary=summary_text[:300],
            new_characters=outline.characters_appearing,
            new_locations=outline.locations,
            key_events=[outline.chapter_objective],
            unresolved_hooks=outline.foreshadowing_plant,
        ).__dict__

    def _run_outline_review(self, outline, chapter_number: int):
        """运行小纲审查"""
        print(SEPARATOR_THIN)
        reviewer = OutlineReviewAgent(self.state.config, self.state.setting_library)
        setting_summary = self.slm.get_summary(
            current_chapter=chapter_number,
            unresolved_hooks=self.state.unresolved_hooks_global,
        )
        unresolved = self._build_unresolved_hooks()

        review_result = reviewer.review(outline, setting_summary, unresolved)

        if not review_result.passed:
            print_warning(f"细纲审查未通过（评分：{review_result.score}/100），建议根据以上问题修改细纲后重试")

    def _run_chapter_writing(self, outline, chapter_number: int):
        """运行章节写作"""
        print(SEPARATOR_THIN)
        writer = ChapterWritingAgent(self.state.config, self.state.setting_library)
        setting_summary = self.slm.get_summary(
            current_chapter=chapter_number,
            unresolved_hooks=self.state.unresolved_hooks_global,
        )
        prev_content_summary = self._build_previous_content_summary(chapter_number)
        prev_chapter_content = self._get_previous_chapter_content(chapter_number)

        return writer.interactive_writing_loop(
            chapter_outline=outline,
            setting_summary=setting_summary,
            previous_content_summary=prev_content_summary,
            previous_chapter_content=prev_chapter_content,
        )

    def _run_content_review(self, chapter_content, outline, chapter_number: int) -> bool:
        """运行文章校验，返回是否通过"""
        print(SEPARATOR_THIN)
        reviewer = ContentReviewAgent(self.state.config, self.state.setting_library)
        setting_summary = self.slm.get_summary(
            current_chapter=chapter_number,
            unresolved_hooks=self.state.unresolved_hooks_global,
        )

        review_result = reviewer.review(chapter_content, outline, setting_summary)
        return review_result.passed

    def _run_setting_maintenance(self, chapter_content, outline, chapter_number: int):
        """运行数据库维护"""
        print(SEPARATOR_THIN)
        maintainer = SettingMaintenanceAgent(self.state.config, self.state.setting_library)
        result = maintainer.update_from_chapter(chapter_content, outline, chapter_number)
        self.pm.save_setting_library()

    def _build_previous_content_summary(self, current_chapter: int) -> str:
        """构建前文内容摘要（供章节写作Agent使用）"""
        if current_chapter == 1:
            return "（这是第一章，无前文）"

        summaries = []
        for ch in range(1, current_chapter):
            key = str(ch)
            if key in self.state.chapter_summaries:
                cs = self.state.chapter_summaries[key]
                if isinstance(cs, dict):
                    title = cs.get("title", f"第{ch}章")
                    summary = cs.get("summary", "")
                else:
                    title = getattr(cs, "title", f"第{ch}章")
                    summary = getattr(cs, "summary", "")
                summaries.append(f"第{ch}章「{title}」：{summary}")

        return "\n".join(summaries[-10:]) if summaries else "（无前文摘要）"

    def _get_previous_chapter_content(self, current_chapter: int) -> str:
        """获取上一章的正文内容（供衔接参考）"""
        if current_chapter <= 1:
            return ""
        prev_key = str(current_chapter - 1)
        if prev_key in self.state.chapter_contents:
            content_data = self.state.chapter_contents[prev_key]
            if isinstance(content_data, dict):
                return content_data.get("content", "")
            return getattr(content_data, "content", "")
        return ""

    def _load_and_continue(self):
        """加载已有项目并继续(经书架选择,支持全部三种格式)"""
        from bookshelf import (
            BookshelfManager, BookshelfUI, FORMAT_MANAGER, FORMAT_CLI, FORMAT_DEMO,
        )

        manager = BookshelfManager()
        manager.scan()
        if not manager.books:
            print_warning("书架上是空的，请先创建新项目")
            self._new_book_wizard()
            return

        ui = BookshelfUI(manager)
        book = ui._select_book("选择要续写的书")
        if not book:
            return

        if book.format == FORMAT_MANAGER:
            manager.mark_opened(book.dir_name)
            self._continue_manager_book(book)
        elif book.format == FORMAT_CLI:
            manager.mark_opened(book.dir_name)
            ui._write_cli_chapter_loop(book)
        else:
            print_warning("Demo 格式是自动演示产物，只读不可续写")
            print(f"  全文见：{os.path.join(book.project_dir, 'manuscript.md')}")


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = NovelAgentApp()
    app.run()
