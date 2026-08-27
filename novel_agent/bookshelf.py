# -*- coding: utf-8 -*-
"""
书架 — 多本小说的记忆与管理系统
================================================================
在不动现有三套存储格式的前提下,提供统一的书架层:
  · 注册表 data/projects/library.json:全部书籍索引 + 「当前书」指针
  · 格式适配器:交互式(ProjectManager 多 JSON)/ CLI(单 project.json)/ Demo
  · 书籍管理:重命名 / 删除(先备份) / 归档 / 标签 / 备份
  · 记忆管理:设定库浏览 / 章节时间线 / 伏笔账本(可标记回收)

入口:
  python bookshelf.py           交互式书架
  python bookshelf.py list      非交互列出全部书籍
"""

import os
import sys
import shutil
import zipfile
import unicodedata
from dataclasses import dataclass, field, fields as dataclass_fields
from datetime import datetime

import config
from models import (
    SettingLibrary,
    CharacterEntry, GeographyEntry, HistoryEntry,
    PowerSystemEntry, FactionEntry,
)
from utils import (
    save_json, load_json,
    print_header, print_subheader, print_warning, print_success, print_error,
    ask, ask_yes_no, ask_choice, press_enter_to_continue,
    SEPARATOR, SEPARATOR_THIN, now_str,
)

# ── 格式常量 ────────────────────────────────────────────────

FORMAT_MANAGER = "manager"   # main.py + ProjectManager 多 JSON 格式
FORMAT_CLI = "cli"           # cli.py 单 project.json 格式(插件路径)
FORMAT_DEMO = "demo"         # write_novel_demo.py 的演示格式(只读)

FORMAT_LABELS = {
    FORMAT_MANAGER: "交互式",
    FORMAT_CLI: "CLI",
    FORMAT_DEMO: "Demo",
}

STATUS_WRITING = "writing"
STATUS_FINISHED = "finished"
STATUS_ARCHIVED = "archived"
STATUS_LABELS = {STATUS_WRITING: "写作中", STATUS_FINISHED: "已完本", STATUS_ARCHIVED: "已归档"}

REGISTRY_FILE = "library.json"

ENTRY_CLASSES = {
    "characters": CharacterEntry, "geography": GeographyEntry,
    "history": HistoryEntry, "power_system": PowerSystemEntry,
    "factions": FactionEntry,
}


# ── 书籍信息 ────────────────────────────────────────────────

@dataclass
class BookInfo:
    """书架上一本书的摘要信息(可从磁盘任意一种格式重建)"""
    dir_name: str
    title: str
    format: str = ""
    genre: str = ""
    style: str = ""
    core_idea: str = ""
    volume: int = 1
    chapter: int = 1
    chapters_written: int = 0
    total_words: int = 0
    setting_counts: dict = field(default_factory=dict)
    setting_total: int = 0
    unresolved_hooks: int = 0
    created_at: str = ""
    updated_at: str = ""
    # 以下三项来自注册表,而非项目文件
    last_opened: str = ""
    status: str = STATUS_WRITING
    tags: list = field(default_factory=list)

    @property
    def project_dir(self) -> str:
        return os.path.join(config.DATA_DIR, self.dir_name)

    @property
    def format_label(self) -> str:
        return FORMAT_LABELS.get(self.format, self.format or "?")

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


# ── 格式适配器(纯读) ──────────────────────────────────────

def detect_format(project_dir: str) -> str | None:
    """根据目录内文件判断项目格式"""
    if os.path.exists(os.path.join(project_dir, "project.json")):
        return FORMAT_CLI
    if os.path.exists(os.path.join(project_dir, "meta.json")):
        return FORMAT_MANAGER
    if os.path.exists(os.path.join(project_dir, "demo_meta.json")):
        return FORMAT_DEMO
    return None


def _safe_dir_name(name: str) -> str:
    """目录安全名(与 ProjectManager._safe_name 同规则)"""
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)
    return safe.strip() or "untitled"


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _dir_created(project_dir: str) -> str:
    try:
        return _fmt_time(os.path.getctime(project_dir))
    except OSError:
        return ""


def _dir_updated(project_dir: str) -> str:
    """目录内最新一次 JSON 文件修改时间"""
    latest = 0.0
    try:
        for name in os.listdir(project_dir):
            if name.endswith(".json"):
                latest = max(latest, os.path.getmtime(os.path.join(project_dir, name)))
    except OSError:
        pass
    return _fmt_time(latest) if latest else ""


def _setting_counts(data: dict) -> tuple[dict, int]:
    """设定库原始数据 {库名: {条目名: ...}} → 各库条数与总数"""
    counts = {name: len(data.get(name) or {}) for name in ENTRY_CLASSES}
    return counts, sum(counts.values())


def read_manager_project(dir_name: str) -> BookInfo:
    """读取 ProjectManager 多 JSON 格式"""
    pdir = os.path.join(config.DATA_DIR, dir_name)
    meta = load_json(os.path.join(pdir, "meta.json")) or {}
    conf = load_json(os.path.join(pdir, "config.json")) or {}
    setting = load_json(os.path.join(pdir, "setting_library.json")) or {}
    contents = load_json(os.path.join(pdir, "chapter_contents.json")) or {}
    counts, setting_total = _setting_counts(setting)
    total_words = 0
    for item in contents.values():
        if isinstance(item, dict):
            total_words += item.get("word_count") or len(item.get("content") or "")
    return BookInfo(
        dir_name=dir_name,
        title=meta.get("project_name") or conf.get("project_name") or dir_name,
        format=FORMAT_MANAGER,
        genre=conf.get("genre", ""),
        style=conf.get("writing_style", ""),
        core_idea=conf.get("core_idea", ""),
        volume=meta.get("current_volume", 1) or 1,
        chapter=meta.get("current_chapter", 1) or 1,
        chapters_written=len(contents),
        total_words=total_words,
        setting_counts=counts,
        setting_total=setting_total,
        unresolved_hooks=len(meta.get("unresolved_hooks_global") or []),
        created_at=_dir_created(pdir),
        updated_at=_dir_updated(pdir),
    )


def read_cli_project(dir_name: str) -> BookInfo:
    """读取 cli.py 单 project.json 格式"""
    pdir = os.path.join(config.DATA_DIR, dir_name)
    state = load_json(os.path.join(pdir, "project.json")) or {}
    conf = state.get("config") or {}
    chapters = state.get("chapters") or []
    counts, setting_total = _setting_counts(state.get("setting_library") or {})
    next_chapter = max((c.get("num", 0) for c in chapters if isinstance(c, dict)), default=0) + 1
    return BookInfo(
        dir_name=dir_name,
        title=state.get("title") or conf.get("project_name") or dir_name,
        format=FORMAT_CLI,
        genre=conf.get("genre", ""),
        style=conf.get("writing_style", ""),
        core_idea=conf.get("core_idea", ""),
        volume=1,
        chapter=next_chapter,
        chapters_written=len(chapters),
        total_words=sum(len((c.get("content") or "")) for c in chapters if isinstance(c, dict)),
        setting_counts=counts,
        setting_total=setting_total,
        unresolved_hooks=len(state.get("unresolved_hooks") or []),
        created_at=_dir_created(pdir),
        updated_at=_dir_updated(pdir),
    )


def read_demo_project(dir_name: str) -> BookInfo:
    """读取 write_novel_demo.py 演示格式(只读)"""
    pdir = os.path.join(config.DATA_DIR, dir_name)
    meta = load_json(os.path.join(pdir, "demo_meta.json")) or {}
    chapters = meta.get("chapters") or []
    counts, setting_total = _setting_counts(load_json(os.path.join(pdir, "setting_library.json")) or {})
    return BookInfo(
        dir_name=dir_name,
        title=meta.get("title") or dir_name,
        format=FORMAT_DEMO,
        genre=meta.get("genre") or "",
        chapter=len(chapters) + 1,
        chapters_written=len(chapters),
        total_words=sum((c.get("words") or 0) for c in chapters if isinstance(c, dict)),
        setting_counts=counts,
        setting_total=setting_total,
        unresolved_hooks=len(meta.get("unresolved_hooks") or []),
        created_at=meta.get("generated_at", ""),
        updated_at=_dir_updated(pdir),
    )


READERS = {
    FORMAT_MANAGER: read_manager_project,
    FORMAT_CLI: read_cli_project,
    FORMAT_DEMO: read_demo_project,
}


def read_book(dir_name: str) -> BookInfo | None:
    """按格式读取一本书的摘要信息"""
    pdir = os.path.join(config.DATA_DIR, dir_name)
    fmt = detect_format(pdir)
    if not fmt:
        return None
    return READERS[fmt](dir_name)


# ── 记忆读取(设定库 / 时间线 / 伏笔) ─────────────────────

def build_setting_library(data: dict) -> SettingLibrary:
    """从 {库名: {条目名: dict}} 原始数据重建 SettingLibrary(三种格式同构)"""
    lib = SettingLibrary()
    for lib_name, cls in ENTRY_CLASSES.items():
        for entry_name, entry in (data.get(lib_name) or {}).items():
            if isinstance(entry, cls):
                getattr(lib, lib_name)[entry_name] = entry
            elif isinstance(entry, dict):
                valid = {f.name for f in dataclass_fields(cls)}
                getattr(lib, lib_name)[entry_name] = cls(
                    **{k: v for k, v in entry.items() if k in valid})
    return lib


def get_setting_library(book: BookInfo) -> SettingLibrary | None:
    """读取某本书的设定库对象(供展示 / 一致性检查)"""
    pdir = book.project_dir
    if book.format == FORMAT_CLI:
        state = load_json(os.path.join(pdir, "project.json")) or {}
        data = state.get("setting_library") or {}
    else:
        data = load_json(os.path.join(pdir, "setting_library.json")) or {}
    return build_setting_library(data)


def get_chapter_timeline(book: BookInfo) -> list[dict]:
    """章节时间线:[{num, title, words, summary}]"""
    pdir = book.project_dir
    timeline = []
    if book.format == FORMAT_MANAGER:
        contents = load_json(os.path.join(pdir, "chapter_contents.json")) or {}
        summaries = load_json(os.path.join(pdir, "chapter_summaries.json")) or {}
        for key, item in contents.items():
            if not isinstance(item, dict):
                continue
            num = item.get("chapter_number") or int(key)
            summary_data = summaries.get(str(num)) or summaries.get(key) or {}
            timeline.append({
                "num": num,
                "title": item.get("title", ""),
                "words": item.get("word_count") or len(item.get("content") or ""),
                "summary": summary_data.get("summary", "") if isinstance(summary_data, dict) else "",
            })
    elif book.format == FORMAT_CLI:
        state = load_json(os.path.join(pdir, "project.json")) or {}
        for c in state.get("chapters") or []:
            if isinstance(c, dict):
                timeline.append({
                    "num": c.get("num", 0),
                    "title": c.get("title", ""),
                    "words": len(c.get("content") or ""),
                    "summary": c.get("summary", ""),
                })
    elif book.format == FORMAT_DEMO:
        meta = load_json(os.path.join(pdir, "demo_meta.json")) or {}
        summaries = meta.get("summaries") or {}
        for c in meta.get("chapters") or []:
            if isinstance(c, dict):
                num = c.get("num", 0)
                timeline.append({
                    "num": num,
                    "title": c.get("title", ""),
                    "words": c.get("words") or 0,
                    "summary": summaries.get(str(num), ""),
                })
    timeline.sort(key=lambda t: t["num"])
    return timeline


def read_hooks(book: BookInfo) -> list[str]:
    """未回收伏笔列表"""
    pdir = book.project_dir
    if book.format == FORMAT_MANAGER:
        meta = load_json(os.path.join(pdir, "meta.json")) or {}
        return list(meta.get("unresolved_hooks_global") or [])
    if book.format == FORMAT_CLI:
        state = load_json(os.path.join(pdir, "project.json")) or {}
        return list(state.get("unresolved_hooks") or [])
    if book.format == FORMAT_DEMO:
        meta = load_json(os.path.join(pdir, "demo_meta.json")) or {}
        return list(meta.get("unresolved_hooks") or [])
    return []


def resolve_hook(book: BookInfo, index: int) -> bool:
    """标记第 index 条伏笔已回收(写回项目文件);Demo 格式只读返回 False"""
    pdir = book.project_dir
    if book.format == FORMAT_MANAGER:
        path, key = os.path.join(pdir, "meta.json"), "unresolved_hooks_global"
    elif book.format == FORMAT_CLI:
        path, key = os.path.join(pdir, "project.json"), "unresolved_hooks"
    else:
        return False
    data = load_json(path) or {}
    hooks = data.get(key) or []
    if not (0 <= index < len(hooks)):
        raise ValueError(f"伏笔序号超出范围: {index}(共 {len(hooks)} 条)")
    data[key] = hooks[:index] + hooks[index + 1:]
    save_json(data, path)
    return True


# ── 书架管理器 ──────────────────────────────────────────────

class BookshelfManager:
    """注册表 + 书籍生命周期管理"""

    def __init__(self):
        self.registry: dict = {"current": "", "books": {}}
        self.books: list[BookInfo] = []
        self.load_registry()

    # ── 注册表 ────────────────────────────────────────────

    @property
    def registry_path(self) -> str:
        return os.path.join(config.DATA_DIR, REGISTRY_FILE)

    def load_registry(self):
        data = load_json(self.registry_path)
        if isinstance(data, dict):
            self.registry = {
                "current": data.get("current", "") or "",
                "books": data.get("books", {}) if isinstance(data.get("books"), dict) else {},
            }

    def save_registry(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        save_json(self.registry, self.registry_path)

    # ── 扫描 ──────────────────────────────────────────────

    def scan(self, save: bool = True) -> list[BookInfo]:
        """扫描 DATA_DIR 下全部书籍,合并注册表附加信息(每次先重载注册表)"""
        self.load_registry()
        books = []
        if os.path.isdir(config.DATA_DIR):
            for name in sorted(os.listdir(config.DATA_DIR)):
                pdir = os.path.join(config.DATA_DIR, name)
                if not os.path.isdir(pdir):
                    continue
                fmt = detect_format(pdir)
                if not fmt:
                    continue
                try:
                    info = READERS[fmt](name)
                except Exception as e:
                    print_warning(f"读取项目「{name}」失败,已跳过: {e}")
                    continue
                extra = self.registry["books"].get(name) or {}
                info.status = extra.get("status", STATUS_WRITING)
                info.tags = list(extra.get("tags") or [])
                info.last_opened = extra.get("last_opened", "")
                books.append(info)
        # 最近更新的在前,归档的沉底(稳定排序)
        books.sort(key=lambda b: b.updated_at or "", reverse=True)
        books.sort(key=lambda b: b.status == STATUS_ARCHIVED)

        self.books = books
        # 清理注册表中已不存在的书
        alive = {b.dir_name for b in books}
        self.registry["books"] = {k: v for k, v in self.registry["books"].items() if k in alive}
        if self.registry["current"] not in alive:
            self.registry["current"] = ""
        if save:
            self.save_registry()
        return books

    def get_book(self, dir_name: str) -> BookInfo | None:
        for b in self.books:
            if b.dir_name == dir_name:
                return b
        return None

    def refresh_book(self, dir_name: str) -> BookInfo | None:
        """重新从磁盘读取一本书"""
        info = read_book(dir_name)
        if info:
            extra = self.registry["books"].get(dir_name) or {}
            info.status = extra.get("status", STATUS_WRITING)
            info.tags = list(extra.get("tags") or [])
            info.last_opened = extra.get("last_opened", "")
        return info

    # ── 当前书指针 ────────────────────────────────────────

    def get_current(self) -> BookInfo | None:
        cur = self.registry.get("current", "")
        return self.get_book(cur) if cur else None

    def set_current(self, dir_name: str, save: bool = True):
        if dir_name and not self.get_book(dir_name):
            raise ValueError(f"书架上没有这本书: {dir_name}")
        self.registry["current"] = dir_name
        if save:
            self.save_registry()

    def mark_opened(self, dir_name: str):
        """打开一本书:设为当前 + 记录时间(先重载注册表,避免覆盖其他实例的修改)"""
        self.load_registry()
        self.set_current(dir_name, save=False)
        entry = self.registry["books"].setdefault(dir_name, {})
        entry["last_opened"] = now_str()
        self.registry["books"][dir_name] = entry
        self.save_registry()

    # ── 管理操作 ──────────────────────────────────────────

    def rename(self, dir_name: str, new_name: str) -> BookInfo:
        """重命名:目录 + 内部 project_name 字段 + 注册表"""
        book = self.get_book(dir_name)
        if not book:
            raise ValueError(f"书架上没有这本书: {dir_name}")
        self.load_registry()
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("新书名不能为空")
        new_dir_name = _safe_dir_name(new_name)
        if new_dir_name != dir_name:
            target = os.path.join(config.DATA_DIR, new_dir_name)
            if os.path.exists(target):
                raise ValueError(f"目录已存在: {new_dir_name},请换一个名字")
            os.rename(book.project_dir, target)

        pdir = os.path.join(config.DATA_DIR, new_dir_name)
        if book.format == FORMAT_MANAGER:
            for fname in ("config.json", "meta.json"):
                path = os.path.join(pdir, fname)
                data = load_json(path)
                if data:
                    data["project_name"] = new_name
                    save_json(data, path)
        elif book.format == FORMAT_CLI:
            path = os.path.join(pdir, "project.json")
            data = load_json(path)
            if data:
                data.setdefault("config", {})["project_name"] = new_name
                save_json(data, path)
        # Demo 格式不改内部文件

        # 注册表迁移
        if dir_name in self.registry["books"]:
            self.registry["books"][new_dir_name] = self.registry["books"].pop(dir_name)
        if self.registry.get("current") == dir_name:
            self.registry["current"] = new_dir_name
        self.save_registry()
        self.scan()
        return self.get_book(new_dir_name)

    def backup(self, dir_name: str) -> str:
        """整本打包为 zip,存到 data/backups/"""
        book = self.get_book(dir_name)
        if not book:
            raise ValueError(f"书架上没有这本书: {dir_name}")
        backup_dir = os.path.join(os.path.dirname(config.DATA_DIR), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = os.path.join(backup_dir, f"{dir_name}_{ts}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(book.project_dir):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    zf.write(fpath, os.path.relpath(fpath, book.project_dir))
        return zip_path

    def delete(self, dir_name: str, backup_first: bool = True) -> str | None:
        """删除一本书(默认先备份),返回备份路径"""
        book = self.get_book(dir_name)
        if not book:
            raise ValueError(f"书架上没有这本书: {dir_name}")
        self.load_registry()
        backup_path = self.backup(dir_name) if backup_first else None
        shutil.rmtree(book.project_dir)
        self.registry["books"].pop(dir_name, None)
        if self.registry.get("current") == dir_name:
            self.registry["current"] = ""
        self.save_registry()
        self.scan()
        return backup_path

    def set_status(self, dir_name: str, status: str):
        if status not in STATUS_LABELS:
            raise ValueError(f"未知状态: {status}")
        self._update_registry_entry(dir_name, status=status)

    def set_tags(self, dir_name: str, tags: list):
        self._update_registry_entry(dir_name, tags=[t for t in tags if str(t).strip()])

    def _update_registry_entry(self, dir_name: str, **updates):
        if not self.get_book(dir_name):
            raise ValueError(f"书架上没有这本书: {dir_name}")
        self.load_registry()
        entry = self.registry["books"].setdefault(dir_name, {})
        entry.update(updates)
        self.registry["books"][dir_name] = entry
        self.save_registry()
        book = self.get_book(dir_name)
        if book:
            for key, val in updates.items():
                setattr(book, key, val)


# ── 表格输出(终端 / cli.py list 共用) ────────────────────

def _disp_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _disp_width(s))


def format_book_table(books: list[BookInfo], current_dir: str = "") -> str:
    """按当前书标记输出对齐表格(考虑中文宽度)"""
    headers = ["", "书名", "格式", "题材·文风", "进度", "章节", "字数", "设定", "伏笔", "状态", "目录名"]
    rows = []
    for b in books:
        rows.append([
            "▶" if b.dir_name == current_dir else "",
            f"《{b.title}》",
            b.format_label,
            f"{b.genre}·{b.style}" if b.genre and b.style else (b.genre or b.style or "-"),
            f"{b.volume}卷{b.chapter}章",
            str(b.chapters_written),
            f"{b.total_words / 1000:.1f}千" if b.total_words >= 1000 else str(b.total_words),
            str(b.setting_total),
            str(b.unresolved_hooks),
            b.status_label,
            b.dir_name,
        ])
    all_rows = [headers] + rows
    widths = [max(_disp_width(r[i]) for r in all_rows) for i in range(len(headers))]
    lines = []
    for idx, row in enumerate(all_rows):
        lines.append("  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)).rstrip())
        if idx == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


# ── 交互式书架 UI ───────────────────────────────────────────

class BookshelfUI:
    """交互式书架主循环"""

    def __init__(self, manager: BookshelfManager | None = None,
                 on_continue_manager=None, on_new_book=None):
        """
        on_continue_manager(book): 交互式格式续写回调(由 main.py 注入;缺省走 main.NovelAgentApp)
        on_new_book():              新建书籍回调(由 main.py 注入;缺省走 main.NovelAgentApp 向导)
        """
        self.mgr = manager or BookshelfManager()
        self.on_continue_manager = on_continue_manager
        self.on_new_book = on_new_book

    # ── 主循环 ────────────────────────────────────────────

    def run(self):
        while True:
            self.mgr.scan()
            current = self.mgr.get_current()
            print_header(f"📚 我的书架({len(self.mgr.books)} 本)"
                         + (f"  当前:《{current.title}》" if current else ""))
            print("  [1] 书籍列表 · 选择/打开续写")
            print("  [2] 新建书籍")
            print("  [3] 记忆管理(设定库 / 时间线 / 伏笔账本 / 一致性)")
            print("  [4] 多书总览统计")
            print("  [5] 书籍维护(重命名 / 归档 / 标签 / 备份 / 删除)")
            print("  [0] 返回")
            choice = ask("\n  请选择", "0")
            if choice == "1":
                book = self._select_book()
                if book:
                    self._book_menu(book)
            elif choice == "2":
                self._new_book()
            elif choice == "3":
                self._memory_menu()
            elif choice == "4":
                self._dashboard()
            elif choice == "5":
                self._maintain_menu()
            elif choice == "0":
                return
            else:
                print_warning("无效选择")

    # ── 通用:选一本书 ────────────────────────────────────

    def _select_book(self, prompt: str = "选择书籍") -> BookInfo | None:
        if not self.mgr.books:
            print_warning("书架上是空的,先新建一本书")
            return None
        self._print_table()
        raw = ask(f"\n  {prompt} [1-{len(self.mgr.books)},0=返回]", "0")
        try:
            idx = int(raw) - 1
        except ValueError:
            print_warning("请输入数字")
            return None
        if idx < 0 or idx >= len(self.mgr.books):
            return None
        return self.mgr.books[idx]

    def _print_table(self):
        print(format_book_table(self.mgr.books, self.mgr.registry.get("current", "")))

    # ── 书籍详情/打开 ─────────────────────────────────────

    def _book_menu(self, book: BookInfo):
        while True:
            book = self.mgr.refresh_book(book.dir_name) or book
            print_header(f"《{book.title}》")
            self._print_book_detail(book)
            print(SEPARATOR_THIN)
            print("  [1] 打开续写" + ("" if book.format != FORMAT_DEMO else "(Demo 格式只读)"))
            print("  [2] 记忆浏览")
            print("  [3] 重命名")
            print("  [4] 归档/恢复")
            print("  [5] 备份 zip")
            print("  [6] 删除")
            print("  [7] 设为当前书")
            print("  [0] 返回书架")
            choice = ask("\n  请选择", "0")
            if choice == "1":
                self._open_book(book)
            elif choice == "2":
                self._memory_for_book(book)
            elif choice == "3":
                self._rename_book(book)
                return
            elif choice == "4":
                self._toggle_archive(book)
                return
            elif choice == "5":
                self._backup_book(book)
            elif choice == "6":
                if self._delete_book(book):
                    return
            elif choice == "7":
                if book.format == FORMAT_DEMO:
                    print_warning("Demo 格式只读,不能设为当前书")
                else:
                    self.mgr.mark_opened(book.dir_name)
                    print_success(f"已设为当前书:《{book.title}》")
            elif choice == "0":
                return

    def _print_book_detail(self, book: BookInfo):
        print(f"  格式:{book.format_label} | 题材:{book.genre or '-'} | 文风:{book.style or '-'}"
              f" | 状态:{book.status_label}")
        print(f"  进度:第 {book.volume} 卷 · 下一章 第 {book.chapter} 章 | 已写 {book.chapters_written} 章"
              f" / {book.total_words} 字")
        c = book.setting_counts
        print(f"  设定库:人物 {c.get('characters', 0)} / 地理 {c.get('geography', 0)} / "
              f"历史 {c.get('history', 0)} / 战力 {c.get('power_system', 0)} / "
              f"势力 {c.get('factions', 0)}")
        print(f"  未回收伏笔 {book.unresolved_hooks} 条 | 最近更新 {book.updated_at or '-'}"
              f" | 最近打开 {book.last_opened or '-'}")
        if book.tags:
            print(f"  标签:{' / '.join(book.tags)}")
        if book.core_idea:
            print(f"  脑洞:{book.core_idea[:70]}")

    # ── 打开续写(按格式路由) ─────────────────────────────

    def _open_book(self, book: BookInfo):
        if book.format == FORMAT_DEMO:
            manuscript = os.path.join(book.project_dir, "manuscript.md")
            print_warning("Demo 格式是自动演示产物,只读不可续写")
            print(f"  全文见:{manuscript}")
            return
        self.mgr.mark_opened(book.dir_name)
        self._warn_if_no_api()
        if book.format == FORMAT_MANAGER:
            if self.on_continue_manager:
                self.on_continue_manager(book)
            else:
                self._continue_manager_default(book)
        else:
            self._write_cli_chapter_loop(book)

    @staticmethod
    def _warn_if_no_api():
        if not config.DEEPSEEK_API_KEY or config.DEEPSEEK_API_KEY == "your-api-key-here":
            print_warning("DEEPSEEK_API_KEY 未配置,续写阶段调用模型会失败(可在 config.py 或环境变量设置)")

    @staticmethod
    def _continue_manager_default(book: BookInfo):
        """独立运行 bookshelf.py 时,交互式格式的书走 main.py 续写流程"""
        from project_manager import ProjectManager
        from setting_library import SettingLibraryManager
        import main as main_module

        app = main_module.NovelAgentApp()
        app.pm = ProjectManager(book.title)
        app.state = app.pm.load(book.title)
        if app.state is None:
            print_error("项目加载失败(meta/config 数据异常)")
            return
        app.slm = SettingLibraryManager(app.state.setting_library, app.state.config)
        app.continue_project()

    def _write_cli_chapter_loop(self, book: BookInfo):
        """CLI 格式:循环生成下一章"""
        from types import SimpleNamespace
        import cli as cli_module

        while True:
            book = self.mgr.refresh_book(book.dir_name) or book
            print_subheader(f"下一章:第 {book.chapter} 章")
            choice = ask("  [生成(g) / 返回(q)]", "q").lower()
            if choice != "g":
                return
            cli_module.cmd_chapter(SimpleNamespace(project_dir=book.project_dir, num=0))
            press_enter_to_continue()

    # ── 新建书籍 ──────────────────────────────────────────

    def _new_book(self):
        if self.on_new_book:
            self.on_new_book()
            return
        # 独立运行:走 main.py 的完整向导
        import main as main_module
        app = main_module.NovelAgentApp()
        app._phase_0_init_project()
        app._phase_1_init_settings()
        app._phase_2_volume_outline()
        app._phase_3_chapter_loop()
        self.mgr.scan()

    # ── 记忆管理 ──────────────────────────────────────────

    def _memory_menu(self):
        book = self._select_book("选择要查看记忆的书籍")
        if book:
            self._memory_for_book(book)

    def _memory_for_book(self, book: BookInfo):
        while True:
            book = self.mgr.refresh_book(book.dir_name) or book
            print_header(f"记忆管理 —《{book.title}》")
            print("  [1] 设定库浏览(人物/地理/历史/战力/势力)")
            print("  [2] 章节时间线")
            print("  [3] 伏笔账本(标记回收)")
            print("  [4] 跨库一致性检查")
            print("  [0] 返回")
            choice = ask("\n  请选择", "0")
            if choice == "1":
                self._browse_settings(book)
            elif choice == "2":
                self._show_timeline(book)
            elif choice == "3":
                self._hooks_ledger(book)
            elif choice == "4":
                self._consistency_check(book)
            elif choice == "0":
                return

    def _browse_settings(self, book: BookInfo):
        from setting_library import SettingLibraryManager
        from models import ProjectConfig

        lib = get_setting_library(book)
        if lib is None:
            print_warning("这本书没有设定库数据")
            return
        slm = SettingLibraryManager(lib, ProjectConfig(project_name=book.title))
        names = slm.get_library_names()
        while True:
            print_subheader("设定库")
            for i, name in enumerate(names, 1):
                print(f"  [{i}] {slm.get_library_label(name)}({slm.get_entry_count(name)} 条)")
            print("  [0] 返回")
            raw = ask("\n  选择库", "0")
            if raw == "0":
                return
            try:
                lib_name = names[int(raw) - 1]
            except (ValueError, IndexError):
                print_warning("无效选择")
                continue
            entries = slm.get_entries(lib_name)
            if not entries:
                print_warning("该库暂无条目")
                continue
            while True:
                print(f"\n  {slm.get_library_label(lib_name)}条目:")
                for i, (ename, e) in enumerate(entries.items(), 1):
                    importance = getattr(e, "importance", "?")
                    print(f"    [{i}] {ename} [{importance}]")
                print("    [0] 返回")
                raw = ask("\n  查看条目详情", "0")
                if raw == "0":
                    break
                try:
                    ename = list(entries.keys())[int(raw) - 1]
                except (ValueError, IndexError):
                    print_warning("无效选择")
                    continue
                print()
                print(slm.display_entry(lib_name, ename))
                press_enter_to_continue()

    def _show_timeline(self, book: BookInfo):
        timeline = get_chapter_timeline(book)
        if not timeline:
            print_warning("这本书还没写过章节")
            return
        print_subheader(f"章节时间线({len(timeline)} 章,共 {sum(t['words'] for t in timeline)} 字)")
        for t in timeline:
            print(f"  第{t['num']:>3}章《{t['title']}》 {t['words']}字")
            if t["summary"]:
                print(f"        {t['summary'][:76]}")
        press_enter_to_continue()

    def _hooks_ledger(self, book: BookInfo):
        while True:
            hooks = read_hooks(book)
            print_subheader(f"伏笔账本 — 未回收 {len(hooks)} 条"
                            + ("(Demo 格式只读)" if book.format == FORMAT_DEMO else ""))
            if not hooks:
                print("  (全部伏笔已回收 ✓)")
                press_enter_to_continue()
                return
            for i, h in enumerate(hooks, 1):
                print(f"  [{i}] {h}")
            if book.format == FORMAT_DEMO:
                press_enter_to_continue()
                return
            raw = ask("\n  输入序号标记回收(0=返回)", "0")
            if raw == "0":
                return
            try:
                idx = int(raw) - 1
                resolve_hook(book, idx)
                print_success(f"已标记回收:{hooks[idx][:50]}…")
                book = self.mgr.refresh_book(book.dir_name) or book
            except (ValueError, IndexError):
                print_warning("无效序号")

    def _consistency_check(self, book: BookInfo):
        from setting_library import SettingLibraryManager
        from models import ProjectConfig

        lib = get_setting_library(book)
        if lib is None:
            print_warning("这本书没有设定库数据")
            return
        print_subheader("跨库一致性检查")
        issues = SettingLibraryManager(lib, ProjectConfig(project_name=book.title)).check_consistency()
        if not issues:
            print_success("未发现一致性问题 ✓")
        else:
            print_warning(f"发现 {len(issues)} 条问题:")
            for issue in issues:
                print(f"  · {issue}")
        press_enter_to_continue()

    # ── 总览统计 ──────────────────────────────────────────

    def _dashboard(self):
        books = [b for b in self.mgr.books if b.status != STATUS_ARCHIVED]
        print_header("多书总览")
        if not books:
            print_warning("书架上没有书")
            return
        total_words = sum(b.total_words for b in books)
        total_chapters = sum(b.chapters_written for b in books)
        total_hooks = sum(b.unresolved_hooks for b in books)
        finished = sum(1 for b in books if b.status == STATUS_FINISHED)
        print(f"  在写书籍 {len(books)} 本(另有归档 {len(self.mgr.books) - len(books)} 本)"
              f" | 完本 {finished} 本")
        print(f"  累计 {total_chapters} 章 / {total_words} 字 | 未回收伏笔 {total_hooks} 条")
        print(SEPARATOR_THIN)
        for b in books:
            print(f"  《{b.title}》 {b.format_label} {b.status_label}"
                  f" — 第{b.volume}卷第{b.chapter}章 / {b.chapters_written}章 / {b.total_words}字"
                  f" / 伏笔{b.unresolved_hooks}")
        press_enter_to_continue()

    # ── 书籍维护 ──────────────────────────────────────────

    def _maintain_menu(self):
        book = self._select_book("选择要维护的书籍")
        if not book:
            return
        while True:
            book = self.mgr.refresh_book(book.dir_name) or book
            print_header(f"维护 —《{book.title}》")
            print(f"  [1] 重命名          [2] {'恢复写作' if book.status == STATUS_ARCHIVED else '归档'}")
            print(f"  [3] {'恢复写作' if book.status == STATUS_FINISHED else '标记完本'}"
                  f"   [4] 标签管理(当前:{' / '.join(book.tags) if book.tags else '无'})")
            print("  [5] 备份 zip        [6] 删除")
            print("  [0] 返回")
            choice = ask("\n  请选择", "0")
            if choice == "1":
                self._rename_book(book)
                return
            elif choice == "2":
                self._toggle_archive(book)
                return
            elif choice == "3":
                new_status = STATUS_WRITING if book.status == STATUS_FINISHED else STATUS_FINISHED
                self.mgr.set_status(book.dir_name, new_status)
                print_success(f"已标记为「{STATUS_LABELS[new_status]}」")
                return
            elif choice == "4":
                self._edit_tags(book)
            elif choice == "5":
                self._backup_book(book)
            elif choice == "6":
                if self._delete_book(book):
                    return
            elif choice == "0":
                return

    def _rename_book(self, book: BookInfo):
        new_name = ask(f"  新书名(当前:{book.title})", "")
        if not new_name.strip():
            print_warning("未输入书名,取消")
            return
        try:
            renamed = self.mgr.rename(book.dir_name, new_name.strip())
            print_success(f"已重命名为《{renamed.title}》(目录 {renamed.dir_name})")
        except ValueError as e:
            print_error(str(e))

    def _toggle_archive(self, book: BookInfo):
        if book.status == STATUS_ARCHIVED:
            self.mgr.set_status(book.dir_name, STATUS_WRITING)
            print_success("已恢复为「写作中」")
        else:
            if ask_yes_no(f"确认归档《{book.title}》?(归档后沉底显示,不参与默认列表)", "n"):
                self.mgr.set_status(book.dir_name, STATUS_ARCHIVED)
                print_success("已归档")

    def _edit_tags(self, book: BookInfo):
        raw = ask("  标签(逗号分隔,直接回车清除)", ",".join(book.tags))
        tags = [t.strip() for t in raw.split(",") if t.strip()] if raw.strip() else []
        self.mgr.set_tags(book.dir_name, tags)
        print_success(f"标签已更新:{' / '.join(tags) if tags else '(无)'}")

    def _backup_book(self, book: BookInfo):
        try:
            path = self.mgr.backup(book.dir_name)
            print_success(f"已备份 → {path}")
        except ValueError as e:
            print_error(str(e))

    def _delete_book(self, book: BookInfo) -> bool:
        print_warning(f"即将删除《{book.title}》(目录 {book.dir_name}),会先自动备份 zip")
        if not ask_yes_no("确认删除?", "n"):
            return False
        typed = ask(f"  请输入书名「{book.title}」再次确认", "")
        if typed.strip() != book.title:
            print_warning("书名不匹配,取消删除")
            return False
        backup_path = self.mgr.delete(book.dir_name)
        if backup_path:
            print_success(f"已删除,备份保存在:{backup_path}")
        else:
            print_success("已删除(未做备份)")
        return True


# ── 入口 ────────────────────────────────────────────────────

def main(argv: list | None = None):
    argv = argv if argv is not None else sys.argv[1:]
    manager = BookshelfManager()
    manager.scan()
    if argv and argv[0] == "list":
        if not manager.books:
            print("书架上是空的,用 python main.py 或 python cli.py new 开新书")
            return
        print(format_book_table(manager.books, manager.registry.get("current", "")))
        return
    BookshelfUI(manager).run()


if __name__ == "__main__":
    main()
