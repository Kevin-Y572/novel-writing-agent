# -*- coding: utf-8 -*-
"""REST API — 书架/书籍详情/设定库/伏笔/导出/任务提交 + WebSocket 进度推送"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import config as engine_config
from bookshelf import (
    BookshelfManager, read_book, get_chapter_timeline, read_hooks,
    get_setting_library, FORMAT_DEMO, _safe_dir_name,
)
from cli import load_state

from .task_manager import TaskManager
from . import pipelines

LIB_LABELS = {"characters": "人物", "geography": "地理", "history": "历史",
              "power_system": "战力", "factions": "势力"}


def _book_json(b) -> dict:
    return {
        "dir_name": b.dir_name, "title": b.title, "format": b.format,
        "format_label": b.format_label, "genre": b.genre, "style": b.style,
        "core_idea": b.core_idea, "chapters_written": b.chapters_written,
        "total_words": b.total_words, "setting_counts": b.setting_counts,
        "unresolved_hooks": b.unresolved_hooks, "status": b.status,
        "status_label": b.status_label, "updated_at": b.updated_at,
    }


def _entries_json(lib, lib_name: str) -> list:
    out = []
    for name, e in getattr(lib, lib_name).items():
        d = {k: v for k, v in e.__dict__.items() if v not in (None, "", [], {})}
        d["_name"] = name
        out.append(d)
    return out


def create_app() -> FastAPI:
    app = FastAPI(title="novel_agent WebUI")
    tm = TaskManager()
    app.state.tm = tm

    # ── 健康 ────────────────────────────────────────────
    @app.get("/api/health")
    def health():
        return {"ok": True,
                "api_key_configured": bool(engine_config.DEEPSEEK_API_KEY)}

    # ── 书架 ────────────────────────────────────────────
    @app.get("/api/books")
    def list_books():
        mgr = BookshelfManager()
        books = mgr.scan(save=False)
        return [_book_json(b) for b in books]

    @app.get("/api/books/{dir_name}")
    def book_detail(dir_name: str):
        book = read_book(dir_name)
        if not book:
            raise HTTPException(404, "书架上没有这本书")
        detail = _book_json(book)
        detail["timeline"] = get_chapter_timeline(book)
        detail["hooks"] = read_hooks(book)
        return detail

    @app.post("/api/books/{dir_name}/open")
    def open_book(dir_name: str):
        mgr = BookshelfManager()
        mgr.scan(save=False)
        if not mgr.get_book(dir_name):
            raise HTTPException(404, "书架上没有这本书")
        mgr.mark_opened(dir_name)
        return {"ok": True}

    # ── 章节正文 ────────────────────────────────────────
    @app.get("/api/books/{dir_name}/chapters/{num}")
    def chapter_content(dir_name: str, num: int):
        book = read_book(dir_name)
        if not book:
            raise HTTPException(404, "书架上没有这本书")
        state = load_state(book.project_dir)
        for c in state["chapters"]:
            if c["num"] == num:
                return c
        raise HTTPException(404, f"第 {num} 章不存在")

    # ── 设定库 ──────────────────────────────────────────
    @app.get("/api/books/{dir_name}/settings")
    def settings(dir_name: str, lib: str | None = None):
        book = read_book(dir_name)
        if not book:
            raise HTTPException(404, "书架上没有这本书")
        sl = get_setting_library(book)
        if sl is None:
            return {}
        names = [lib] if lib else list(LIB_LABELS)
        out = {}
        for n in names:
            if n not in LIB_LABELS:
                raise HTTPException(400, f"未知子库:{n}")
            out[n] = _entries_json(sl, n)
        return out

    # ── 伏笔 ────────────────────────────────────────────
    @app.get("/api/books/{dir_name}/hooks")
    def hooks(dir_name: str):
        book = read_book(dir_name)
        if not book:
            raise HTTPException(404, "书架上没有这本书")
        return {"unresolved": read_hooks(book)}

    # ── 导出 ────────────────────────────────────────────
    @app.get("/api/books/{dir_name}/export")
    def export(dir_name: str):
        book = read_book(dir_name)
        if not book:
            raise HTTPException(404, "书架上没有这本书")
        state = load_state(book.project_dir)
        parts = [f"# {state['title']}\n", f"> {state['blurb']}\n"]
        for c in state["chapters"]:
            parts.append(f"\n---\n\n## 第{c['num']}章 {c['title']}\n\n{c['content']}\n")
        content = "\n".join(parts)
        fd, path = tempfile.mkstemp(suffix=".md", prefix=f"{dir_name}_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        return FileResponse(path, filename=f"{state['title']}.md",
                            media_type="text/markdown")

    # ── 任务 ────────────────────────────────────────────
    def _unique_dir_name(base: str) -> str:
        """在 DATA_DIR 下生成不冲突的目录名"""
        safe = _safe_dir_name(base[:16].strip() or "untitled")
        candidate, i = safe, 1
        while os.path.exists(os.path.join(engine_config.DATA_DIR, candidate)):
            i += 1
            candidate = f"{safe}_{i}"
        return candidate

    @app.post("/api/tasks/new-book")
    def task_new_book(body: dict):
        if not body.get("idea"):
            raise HTTPException(400, "缺少必填字段 idea(核心脑洞)")
        project_dir = body.get("project_dir") or os.path.join(
            engine_config.DATA_DIR,
            _unique_dir_name(body.get("name") or body["idea"]))
        try:
            task = tm.submit(
                "new_book", f"新书《{body.get('name') or body['idea'][:12]}》",
                lambda emit: pipelines.new_book(
                    project_dir=project_dir, idea=body["idea"],
                    genre=body.get("genre", "玄幻"), style=body.get("style", "番茄爆款"),
                    person=body.get("person", "第三人称"), slang=body.get("slang", "中"),
                    words=int(body.get("words", 3000)), emit=emit,
                    name=body.get("name", ""), core_setting=body.get("core_setting", "")))
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return task.to_dict()

    @app.post("/api/tasks/write-chapter")
    def task_write_chapter(body: dict):
        dir_name = body.get("dir_name")
        if not dir_name:
            raise HTTPException(400, "缺少 dir_name")
        book = read_book(dir_name)
        if not book:
            raise HTTPException(404, "书架上没有这本书")
        if book.format == FORMAT_DEMO:
            raise HTTPException(400, "Demo 格式只读,不能写章节")
        try:
            task = tm.submit(
                "write_chapter", f"《{book.title}》下一章",
                lambda emit: pipelines.write_chapter(
                    project_dir=book.project_dir,
                    num=int(body.get("num", 0)), emit=emit))
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return task.to_dict()

    @app.get("/api/tasks/current")
    def task_current():
        t = tm.current()
        return t.to_dict() if t else None

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str):
        t = tm.get(task_id)
        if not t:
            raise HTTPException(404, "任务不存在")
        d = t.to_dict()
        d["events"] = t.events
        return d

    # ── WebSocket 进度推送 ──────────────────────────────
    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def on_event(event: dict):
            # TaskManager 在工作线程广播,需线程安全地投递到事件循环
            loop.call_soon_threadsafe(queue.put_nowait, event)

        unsub = tm.subscribe(on_event)
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            unsub()

    # ── 静态前端(Task 6 提供 index.html) ───────────────
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    if os.path.isdir(static_dir):
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app
