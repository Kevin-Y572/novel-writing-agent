# -*- coding: utf-8 -*-
"""后台任务管理 — 单并发 + 事件队列

任务函数签名: fn(emit) -> result，emit(dict) 推事件（自动补 task_id/ts）。
事件经 subscriber 广播（WebSocket 用），同时存入 task.events 供 REST 补偿拉取。
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Task:
    id: str
    kind: str                      # new_book / write_chapter
    label: str
    status: TaskStatus = TaskStatus.PENDING
    result: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    thread: Optional[threading.Thread] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label,
            "status": self.status.value, "result": self.result,
            "error": self.error, "created_at": self.created_at,
        }


class TaskManager:
    """全局单并发：同一时间最多一个任务在跑（符合本地单机约束）"""

    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[dict], None]] = []

    # ── 订阅（WebSocket 端点注册回调） ──────────────────
    def subscribe(self, cb: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(cb)

        def unsub():
            with self._lock:
                if cb in self._subscribers:
                    self._subscribers.remove(cb)

        return unsub

    def _broadcast(self, event: dict):
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(event)
            except Exception:
                pass  # 订阅方异常不拖垮任务

    # ── 任务提交与查询 ──────────────────────────────────
    def submit(self, kind: str, label: str, fn: Callable) -> Task:
        with self._lock:
            running = [t for t in self._tasks.values()
                       if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)]
            if running:
                raise RuntimeError(f"已有任务在运行:{running[0].label}")
            task = Task(id=uuid.uuid4().hex[:12], kind=kind, label=label)
            self._tasks[task.id] = task
        task.thread = threading.Thread(target=self._run, args=(task, fn), daemon=True)
        task.thread.start()
        return task

    def _run(self, task: Task, fn: Callable):
        task.status = TaskStatus.RUNNING
        self._broadcast({"type": "task_started", "task_id": task.id,
                         "kind": task.kind, "label": task.label})

        def emit(event: dict):
            event = dict(event)
            event.setdefault("task_id", task.id)
            event.setdefault("ts", time.time())
            task.events.append(event)
            self._broadcast(event)

        try:
            task.result = fn(emit) or {}
            task.status = TaskStatus.DONE
            self._broadcast({"type": "task_done", "task_id": task.id,
                             "ts": time.time(), "result": task.result})
        except Exception as e:  # 引擎任何异常都转为失败事件
            task.status = TaskStatus.FAILED
            task.error = str(e)
            import traceback
            detail = traceback.format_exc()
            task.events.append({"type": "error", "task_id": task.id,
                                "ts": time.time(), "message": str(e), "traceback": detail})
            self._broadcast({"type": "error", "task_id": task.id,
                             "ts": time.time(), "message": str(e), "traceback": detail})

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def current(self) -> Optional[Task]:
        with self._lock:
            for t in self._tasks.values():
                if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                    return t
        return None

    def events(self, task_id: str) -> list:
        t = self._tasks.get(task_id)
        return list(t.events) if t else []

    def wait(self, task_id: str, timeout: float = 60):
        t = self._tasks.get(task_id)
        if t and t.thread:
            t.thread.join(timeout)
