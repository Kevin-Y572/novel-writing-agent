"""
工具函数 — 打印美化、JSON 处理、用户输入
"""

import json
import os
import re
from datetime import datetime
from typing import Any


# ═══════════════════════════════════════════════════════════════
# 终端美化输出
# ═══════════════════════════════════════════════════════════════

SEPARATOR = "═" * 72
SEPARATOR_THIN = "─" * 72
SEPARATOR_DOT = "·" * 72


def print_header(title: str):
    """打印主标题"""
    print(f"\n{SEPARATOR}")
    print(f"  {title}")
    print(SEPARATOR)


def print_subheader(title: str):
    """打印子标题"""
    print(f"\n{SEPARATOR_THIN}")
    print(f"  ▶ {title}")
    print(SEPARATOR_THIN)


def print_section(title: str):
    """打印小节标题"""
    print(f"\n  ▸ {title}")

def print_info(key: str, value: str):
    """打印键值对"""
    if value:
        print(f"    {key}: {value}")

def print_list(items: list, indent: int = 4):
    """打印列表"""
    prefix = " " * indent
    for i, item in enumerate(items, 1):
        if isinstance(item, dict):
            dict_str = " | ".join(f"{k}: {v}" for k, v in item.items())
            print(f"{prefix}[{i}] {dict_str}")
        else:
            print(f"{prefix}[{i}] {str(item)}")

def print_warning(msg: str):
    print(f"\n  ⚠ {msg}")

def print_success(msg: str):
    print(f"  ✓ {msg}")

def print_error(msg: str):
    print(f"  ✗ {msg}")


# ═══════════════════════════════════════════════════════════════
# JSON 文件 I/O
# ═══════════════════════════════════════════════════════════════

def save_json(data: Any, filepath: str) -> str:
    """保存数据为 JSON 文件（原子写：先写临时文件再替换，崩溃不会损坏原文件）"""
    directory = os.path.dirname(filepath)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = filepath + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, filepath)
    return filepath


def load_json(filepath: str) -> dict | None:
    """加载 JSON 文件，不存在返回 None；损坏时告警并返回 None（不抛异常）"""
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        print_warning(f"JSON 文件损坏，已忽略: {filepath} ({e})")
        return None


def to_dict(obj: Any) -> dict:
    """将 dataclass/对象转换为 dict，处理嵌套"""
    if hasattr(obj, "__dataclass_fields__"):
        result = {}
        for field_name in obj.__dataclass_fields__:
            value = getattr(obj, field_name)
            result[field_name] = _serialize_value(value)
        return result
    return obj


def _serialize_value(value: Any) -> Any:
    """递归序列化值"""
    if hasattr(value, "__dataclass_fields__"):
        return to_dict(value)
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize_value(v) for v in value]
    return value


# ═══════════════════════════════════════════════════════════════
# 用户输入辅助
# ═══════════════════════════════════════════════════════════════

def ask(prompt: str, default: str = "") -> str:
    """带默认值的输入"""
    if default:
        result = input(f"  {prompt} [{default}]: ").strip()
        return result if result else default
    return input(f"  {prompt}: ").strip()


def ask_yes_no(prompt: str, default: str = "y") -> bool:
    """是/否 确认"""
    hint = "[Y/n]" if default == "y" else "[y/N]"
    result = input(f"  {prompt} {hint}: ").strip().lower()
    if not result:
        result = default
    return result in ("y", "yes", "是")


def ask_choice(prompt: str, options: list, default_idx: int = 0) -> str:
    """从列表中选择一项"""
    print(f"\n  {prompt}")
    for i, opt in enumerate(options, 1):
        marker = " (默认)" if i == default_idx + 1 else ""
        print(f"    [{i}] {opt}{marker}")
    while True:
        choice = input(f"  请选择 [1-{len(options)}]: ").strip()
        if not choice:
            return options[default_idx]
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass
        print(f"  无效选择，请输入 1-{len(options)}")


def ask_multiline(prompt: str) -> str:
    """多行输入（输入空行结束）"""
    print(f"\n  {prompt}")
    print(f"  （输入内容后按回车，再按一次空回车结束）")
    lines = []
    while True:
        line = input()
        if line == "" and lines:
            break
        if line:
            lines.append(line)
    return "\n".join(lines)


def press_enter_to_continue():
    """暂停等待用户按回车"""
    input(f"\n  [按回车继续...]")


def now_str() -> str:
    """当前时间字符串"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
