"""
伏笔台账 — 全局伏笔的 ID 化追踪
=================================================
替代原先的自由文本列表 + 子串匹配去重（换皮重复去不掉，长跑必然膨胀）。
核心为纯 Python 确定性逻辑（bigram 相似度），LLM 辅助函数可选启用：
  - llm_merge_duplicates(): 设伏入账后判断新伏笔与现有 open 伏笔是否换皮重复
  - llm_map_recoveries():   把本章"伏笔回收"描述映射到 open 伏笔 ID
LLM 调用失败时自动退化为纯确定性匹配，不阻塞流水线。
"""

from models import Foreshadow
from utils import print_warning

# ── 阈值 ─────────────────────────────────────────────────────
DUPLICATE_THRESHOLD = 0.55   # bigram 重叠系数超过此值视为同一伏笔
RECOVER_THRESHOLD = 0.45     # 回收匹配阈值（回收描述通常更短，放宽）
STALE_CHAPTERS = 30          # 超过 N 章未被提及的 open 伏笔转入归档
MAX_INJECT = 15              # 注入 prompt 的 open 伏笔条数上限
MIN_LIFESPAN = 3             # 长线伏笔设下后至少 N 章内不得回收（防"同章回收"）


def _bigrams(text: str) -> set:
    """中文字符 2-gram 集合（过滤空白）"""
    text = "".join(text.split())
    if len(text) < 2:
        return set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def similarity(a: str, b: str) -> float:
    """重叠系数 = |A∩B| / min(|A|,|B|)。对"长文本包含短文本"的换皮重复友好。"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


def _share_long_substring(a: str, b: str, min_len: int = 4) -> bool:
    """两文本是否共享 ≥min_len 的连续片段（如'连接着封印'）。
    比 bigram 更精确的强信号，用于回收匹配。"""
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    shorter = "".join(shorter.split())
    longer = "".join(longer.split())
    if len(shorter) < min_len:
        return False
    for i in range(len(shorter) - min_len + 1):
        if shorter[i:i + min_len] in longer:
            return True
    return False


class ForeshadowRegistry:
    """全局伏笔台账（id -> Foreshadow）"""

    def __init__(self):
        self.items: dict[str, Foreshadow] = {}

    # ── 持久化 ─────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {fid: vars(f) for fid, f in self.items.items()}

    @classmethod
    def from_dict(cls, data: dict) -> "ForeshadowRegistry":
        reg = cls()
        for fid, val in (data or {}).items():
            if isinstance(val, dict):
                val = {**val, "id": fid}
                reg.items[fid] = Foreshadow(**val)
        return reg

    # ── 查询 ───────────────────────────────────────────────
    def next_id(self) -> str:
        nums = []
        for fid in self.items:
            try:
                nums.append(int(fid.split("-")[-1]))
            except (ValueError, IndexError):
                pass
        return f"F-{max(nums, default=0) + 1:03d}"

    def open_items(self) -> list:
        """open 状态的伏笔，按最近提及排序（新的在后）"""
        items = [f for f in self.items.values() if f.status == "open"]
        items.sort(key=lambda f: (f.last_mention_chapter or f.planted_chapter))
        return items

    def stats(self) -> dict:
        open_n = sum(1 for f in self.items.values() if f.status == "open")
        recovered_n = sum(1 for f in self.items.values() if f.status == "recovered")
        archived_n = sum(1 for f in self.items.values() if f.status == "archived")
        return {"total": len(self.items), "open": open_n,
                "recovered": recovered_n, "archived": archived_n}

    # ── 匹配 ───────────────────────────────────────────────
    def _best_match(self, text: str, statuses=("open",)) -> tuple:
        best, best_sim = None, 0.0
        for f in self.items.values():
            if f.status not in statuses:
                continue
            sim = similarity(text, f.text)
            if sim > best_sim:
                best, best_sim = f, sim
        return best, best_sim

    # ── 设伏入账 ───────────────────────────────────────────
    def register(self, text: str, chapter: int, sticky: bool = False) -> tuple:
        """登记一条伏笔。与现有 open 伏笔 bigram 相似则视为同一伏笔（不新建）。
        sticky=True 为卷级/跨卷主线伏笔（禁模糊回收）。
        返回 (Foreshadow, 是否新建)。"""
        text = (text or "").strip()
        if not text:
            return None, False
        best, sim = self._best_match(text, statuses=("open",))
        if best and sim >= DUPLICATE_THRESHOLD:
            best.mentions += 1
            best.last_mention_chapter = chapter
            return best, False
        f = Foreshadow(id=self.next_id(), text=text, planted_chapter=chapter,
                       last_mention_chapter=chapter, sticky=sticky)
        self.items[f.id] = f
        return f, True

    def register_many(self, texts: list, chapter: int, sticky: bool = False) -> dict:
        """批量登记，返回 {"added": [id], "merged": [id]}"""
        added, merged = [], []
        for t in texts or []:
            f, is_new = self.register(t, chapter, sticky=sticky)
            if f and is_new:
                added.append(f.id)
            elif f:
                merged.append(f.id)
        return {"added": added, "merged": merged}

    def merge(self, dup_id: str, kept_id: str):
        """把 dup_id 并入 kept_id（LLM 判定换皮重复时使用）"""
        dup = self.items.get(dup_id)
        kept = self.items.get(kept_id)
        if dup and kept and dup_id != kept_id:
            kept.mentions += 1
            self.items.pop(dup_id, None)

    # ── 回收 ───────────────────────────────────────────────
    def _apply_recovery(self, f: Foreshadow, chapter: int, explicit: bool) -> Foreshadow | None:
        """应用回收。护栏：
        - 设下未满 MIN_LIFESPAN 章的伏笔一律不回收（硬规则，显式/模糊都不豁免），
          仅记一次推进——防"同章回收/隔章回收"，长线悬念必须养住
        - sticky（卷级主线）只允许显式 ID 回收（粗纲规划的闭环），模糊匹配不动
        返回被回收的伏笔；被护栏拦下时返回 None。"""
        if f is None or f.status != "open":
            return None
        if chapter - f.planted_chapter < MIN_LIFESPAN:
            f.mentions += 1
            f.last_mention_chapter = chapter
            return None
        if f.sticky and not explicit:
            f.mentions += 1
            f.last_mention_chapter = chapter
            return None
        f.status = "recovered"
        f.recovered_chapter = chapter
        f.last_mention_chapter = chapter
        return f

    def recover(self, text: str, chapter: int) -> Foreshadow | None:
        """按描述回收一条 open 伏笔（模糊匹配，受护栏约束）。"""
        text = (text or "").strip()
        if not text:
            return None
        best, best_sim = self._best_match(text, statuses=("open",))
        if best and best_sim >= RECOVER_THRESHOLD:
            # 命中即结束（含被护栏拦下），不再走子串通道重复计推进
            return self._apply_recovery(best, chapter, explicit=False)
        # 二次尝试：强共现片段（回收描述常比伏笔原文短，bigram 重叠系数会低估）
        for f in self.items.values():
            if f.status == "open" and _share_long_substring(text, f.text):
                return self._apply_recovery(f, chapter, explicit=False)
        return None

    def recover_by_id(self, fid: str, chapter: int, explicit: bool = True) -> Foreshadow | None:
        """按 ID 显式回收（LLM 判定同一谜团时的通道，穿透护栏）。"""
        f = self.items.get(fid)
        return self._apply_recovery(f, chapter, explicit=explicit) if f else None

    # ── 归档 ───────────────────────────────────────────────
    def archive_stale(self, current_chapter: int, stale_after: int = STALE_CHAPTERS) -> list:
        """长期未推进的 open 伏笔转入归档（保留追踪，不再注入 prompt 提醒清单）"""
        archived = []
        for f in self.items.values():
            if f.status != "open":
                continue
            last = f.last_mention_chapter or f.planted_chapter
            if current_chapter - last > stale_after:
                f.status = "archived"
                archived.append(f.id)
        return archived

    # ── prompt 注入 ────────────────────────────────────────
    def open_texts(self, max_items: int = MAX_INJECT) -> list:
        """最近活跃的 open 伏笔描述（供 get_summary 伏笔保护与细纲提醒）"""
        items = self.open_items()
        recent = items[-max_items:] if len(items) > max_items else items
        return [f.text for f in recent]

    def hooks_text(self, current_chapter: int, max_items: int = MAX_INJECT) -> str:
        """注入细纲生成的【当前全局未回收伏笔】文本。
        未满 MIN_LIFESPAN 章的标注"只能推进"，卷级主线标注"跨卷主线"。"""
        items = self.open_items()
        if not items:
            return "（暂无）"
        recent = items[-max_items:]
        lines = []
        for f in recent:
            tags = []
            if current_chapter - f.planted_chapter < MIN_LIFESPAN:
                tags.append("未满3章，本章只能推进不得回收")
            if f.sticky:
                tags.append("跨卷主线")
            tag_str = f"〔{'；'.join(tags)}〕" if tags else ""
            lines.append(f"- {f.id}（第{f.planted_chapter}章设下）{tag_str}：{f.text}")
        if len(items) > max_items:
            lines.append(f"（另有 {len(items) - max_items} 条较早伏笔已省略）")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# LLM 辅助（可选，失败自动退化为纯确定性匹配）
# ═══════════════════════════════════════════════════════════════

def llm_merge_duplicates(registry: ForeshadowRegistry, new_ids: list) -> list:
    """对刚入账的新伏笔与既有 open 伏笔做一次换皮重复判断。
    仅当新伏笔与既有伏笔被判定为"同一谜团"时合并。返回合并对 [(dup_id, kept_id)]。"""
    if not new_ids:
        return []
    new_items = [(i, registry.items[i].text) for i in new_ids if i in registry.items]
    existing = [(f.id, f.text) for f in registry.open_items()
                if f.id not in new_ids]
    if not new_items or not existing:
        return []

    from api_client import get_client
    new_block = "\n".join(f"{i}: {t}" for i, t in new_items)
    old_block = "\n".join(f"{i}: {t}" for i, t in existing)
    system = ("你是长篇小说的伏笔管理员。判断新设下的伏笔与现有未回收伏笔是否指向"
              "同一个谜团/同一件事（同一伏笔的换皮重复表述）。注意：涉及同一角色"
              "但谜团不同的不算重复。输出严格 JSON：{\"duplicates\": "
              "[{\"new\": \"F-010\", \"existing\": \"F-003\"}]}，无重复则 duplicates 为空数组。")
    user = (f"【现有未回收伏笔】\n{old_block}\n\n【本章新设下的伏笔】\n{new_block}\n\n"
            f"请判断新伏笔中哪些与现有伏笔重复，直接输出 JSON。")
    try:
        result = get_client().chat_with_json_output(system, user,
                                                    temperature=0.1, max_tokens=4096)
        pairs = []
        for d in result.get("duplicates", []):
            new_id, kept_id = d.get("new"), d.get("existing")
            if new_id in registry.items and kept_id in registry.items and new_id != kept_id:
                pairs.append((new_id, kept_id))
        for dup_id, kept_id in pairs:
            registry.merge(dup_id, kept_id)
        return pairs
    except Exception as e:
        print_warning(f"伏笔 LLM 去重失败（退化为确定性匹配）: {e}")
        return []


def llm_map_recoveries(registry: ForeshadowRegistry, recover_texts: list, chapter: int) -> dict:
    """把本章细纲的"伏笔回收"描述映射到 open 伏笔 ID（LLM 判定，确定性兜底）。
    返回 {"recovered": [id], "unmatched": [描述]}。"""
    unmatched = [t for t in (recover_texts or []) if t and t.strip()]
    if not unmatched or not registry.open_items():
        return {"recovered": [], "unmatched": unmatched}

    from api_client import get_client
    open_block = "\n".join(f"{f.id}: {f.text}" for f in registry.open_items())
    rec_block = "\n".join(f"{i}. {t}" for i, t in enumerate(unmatched))
    system = ("你是长篇小说的伏笔管理员。下面是现有未回收伏笔清单和本章声称要回收的"
              "伏笔描述。请把每条回收描述匹配到对应的伏笔 ID；仅当明确是同一谜团的"
              "揭晓/兑现时才匹配，找不到对应则填 null。输出严格 JSON："
              "{\"mappings\": [{\"recover\": \"描述原文\", \"id\": \"F-003 或 null\"}]}。")
    user = (f"【现有未回收伏笔】\n{open_block}\n\n【本章回收描述】\n{rec_block}\n\n"
            f"请输出映射 JSON。")
    recovered_ids = []
    try:
        result = get_client().chat_with_json_output(system, user,
                                                    temperature=0.1, max_tokens=4096)
        for m in result.get("mappings", []):
            text, fid = m.get("recover", ""), m.get("id")
            f = registry.recover_by_id(fid, chapter) if fid else None
            if f and text in unmatched:
                recovered_ids.append(f.id)
                unmatched.remove(text)
    except Exception as e:
        print_warning(f"伏笔回收 LLM 映射失败（退化为确定性匹配）: {e}")

    # 确定性兜底：LLM 没匹配上的，用 bigram 再试一次
    for text in list(unmatched):
        f = registry.recover(text, chapter)
        if f:
            recovered_ids.append(f.id)
            unmatched.remove(text)
    return {"recovered": recovered_ids, "unmatched": unmatched}
