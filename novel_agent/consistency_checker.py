"""
确定性一致性校验器 — 纯规则，不调 API
=================================================
在正文落盘前拦截硬性矛盾（死人出场、未登记境界词），
并把细纲里带装饰的人物/地点名（如"林上（主角）"）规范化为裸名称。
有误报就放宽规则，宁可漏报不可阻塞流水线。
"""

import re
from setting_library import is_dead, RECALL_CONTEXT_KEYWORDS

# ── 名称规范化 ───────────────────────────────────────────────

_DECORATION_RE = re.compile(r"[（(][^）)]*[）)]")


def normalize_name(raw: str) -> str:
    """去掉名称里的括号装饰、空白与书名号：'林上（主角）' → '林上'"""
    if not raw:
        return ""
    name = _DECORATION_RE.sub("", str(raw))
    name = name.replace("《", "").replace("》", "").strip()
    # 去掉名称尾部残留的连接符
    return name.strip(" ··-—")


def normalize_outline_names(outline) -> int:
    """规范化细纲的 characters_appearing / locations（原地修改），返回修改条数"""
    changed = 0
    for attr in ("characters_appearing", "locations"):
        raw_list = getattr(outline, attr, None) or []
        cleaned = []
        for item in raw_list:
            norm = normalize_name(item)
            if norm and norm not in cleaned:
                cleaned.append(norm)
                if norm != item:
                    changed += 1
            elif not norm:
                changed += 1
        setattr(outline, attr, cleaned)
    return changed


# ── 死人出场扫描 ─────────────────────────────────────────────

# 上下文出现这些词 → 该处提及是"死者遗产/痕迹"，降级为 warning（剧情合法，如死兽留下的印记）
LEGACY_KEYWORDS = ("印记", "残纹", "遗产", "遗物", "遗骸", "尸体", "尸骨",
                   "留下", "生前", "死前", "之死", "墓", "碑", "残魂", "血迹")


def check_dead_characters(content_text: str, setting_library, window: int = 40) -> list:
    """扫描正文中死亡/退场角色的出场。每处出现需在 ±window 字符内带有
    回忆/闪回上下文关键词，否则记违规；遗产/痕迹类提及降级为 warning。"""
    violations = []
    for name, entry in setting_library.characters.items():
        if len(name) < 2 or not is_dead(getattr(entry, "current_status", "")):
            continue
        start = 0
        while True:
            idx = content_text.find(name, start)
            if idx < 0:
                break
            context = content_text[max(0, idx - window): idx + len(name) + window]
            # 判定顺序：回忆/闪回（合法出场）> 遗产/痕迹（warning）> 裸出场（error）
            if any(k in context for k in RECALL_CONTEXT_KEYWORDS):
                start = idx + len(name)
                continue
            if any(k in context for k in LEGACY_KEYWORDS):
                violations.append({
                    "type": "dead_character",
                    "severity": "warning",
                    "description": f"已退场角色「{name}」以遗产/痕迹形式被提及（合法剧情，仅记录）",
                })
                break
            violations.append({
                "type": "dead_character",
                "severity": "error",
                "description": f"已退场角色「{name}」在正文中出现但上下文无回忆/闪回标记",
            })
            break  # 每个角色只报一次
    return violations


# ── 境界词合法性 ─────────────────────────────────────────────

# 常见汉语固定词，含这些的不当作修炼境界（子串级过滤，命中即放行）
_GENERIC_WORDS = {
    "困境", "环境", "边境", "边界", "心境", "情境", "意境", "处境", "绝境",
    "逆境", "顺境", "幻境", "梦境", "胜境", "止境", "压境", "临境", "越境",
    "出境", "入境", "国境", "家境", "化境", "意识", "时间", "时期", "期间",
    "期待", "周期", "初期", "中期", "后期", "前期", "限期", "长期", "短期",
    "学期", "阶段", "阶层", "顶层", "高层", "底层", "上层", "中层", "层层",
    "云层", "煤层", "表层", "一层", "下层", "外在", "内在", "存在", "现在",
    "无限", "有限", "台阶", "阶梯", "阶级", "音阶", "官阶", "段位",
    "石阶", "泥层", "岩层", "土层", "冰层", "雪层", "楼层", "断层", "基层",
}
# 序数/方位/程度字：从候选前缀尾部剥离后再匹配已知境界名
_STRIP_TAIL = set("一二三四五六七八九十百千万第上中下最初中末高顶低大小")

_LEVEL_CAND_RE = re.compile(r"([\u4e00-\u9fa5]{1,4})(境|期|层|阶)")


def known_level_names(setting_library) -> set:
    """从战力设定库提取全部已知境界名（各体系 levels 的 name 字段）"""
    names = set()
    for ps in setting_library.power_system.values():
        for level in (getattr(ps, "levels", None) or []):
            if isinstance(level, dict):
                level_name = level.get("name") or level.get("level") or ""
            else:
                level_name = str(level)
            if level_name:
                names.add(str(level_name).strip())
    return names


def check_power_terms(content_text: str, setting_library) -> list:
    """扫描正文中的疑似境界词（X境/X期/X层/X阶），不在设定库境界表中则标记。
    规则保守：单字前缀、纯方位序数词、通用词一律放过，避免误报阻塞。"""
    known = known_level_names(setting_library)
    if not known:
        return []  # 设定库没有境界数据时不做该项检查

    def is_known(candidate_prefix: str) -> bool:
        # 剥离尾部序数/方位字后再对照（"魂徒三"→"魂徒"）
        core = candidate_prefix
        while core and core[-1] in _STRIP_TAIL:
            core = core[:-1]
        if not core:
            return True  # 纯方位/序数表述（第一层、最上层），放行
        return any(k in core or core in k for k in known)

    violations, seen = [], set()
    for match in _LEVEL_CAND_RE.finditer(content_text):
        candidate = match.group(0)
        prefix = match.group(1)
        # 子串级通用词过滤（正则贪婪前缀可能吞掉通用词，如"第一层台阶"）
        if any(g in candidate for g in _GENERIC_WORDS):
            continue
        if "的" in prefix or "之" in prefix:
            continue  # 描述性短语（"红色的泥层""门前的石阶"），非境界名
        if len(prefix) < 2:  # 单字前缀无法判断，放行
            continue
        if not is_known(prefix) and candidate not in seen:
            seen.add(candidate)
            violations.append({
                "type": "power_term",
                "severity": "warning",
                "description": f"正文出现疑似未登记的境界词「{candidate}」，设定库境界表中无此层级",
            })
    return violations


# ── 正文中的"第X章"引用（机器痕迹，error 级）───────────────

_CHAPTER_REF_RE = re.compile(r"第\s*\d+\s*章")


def check_chapter_refs(content_text: str) -> list:
    """正文中出现"第N章"字样 = 把资料编号写进了故事，人类作者不会这样写。
    一律 error 级（触发强制重写）。"""
    violations = []
    seen = set()
    for m in _CHAPTER_REF_RE.finditer(content_text):
        ref = m.group(0)
        if ref not in seen:
            seen.add(ref)
            ctx = content_text[max(0, m.start() - 20):m.end() + 20].replace("\n", " ")
            violations.append({
                "type": "chapter_ref",
                "severity": "error",
                "description": f"正文出现章节号引用「{ref}」（上下文：…{ctx}…），"
                               f"必须改为故事内时间/事件指代（如'上次''当初''遗迹深层那次'）",
            })
    return violations


# ── 性别代词一致性（同句级，warning）─────────────────────────

_SENT_SPLIT_RE = re.compile(r"[。！？；\n]")


def check_gender_pronouns(content_text: str, setting_library) -> list:
    """同句中出现角色名与其登记性别相反的代词 → warning。
    保守规则：同句还出现其他已登记的异性角色时跳过（代词可能指对方）。"""
    violations = []
    sentences = [s for s in _SENT_SPLIT_RE.split(content_text) if len(s) >= 4]
    for name, entry in setting_library.characters.items():
        gender = (getattr(entry, "gender", "") or "").strip()
        if gender not in ("男", "女") or len(name) < 2:
            continue
        wrong = "她" if gender == "男" else "他"
        for s in sentences:
            if name not in s or wrong not in s:
                continue
            has_other_gender = False
            for other, other_entry in setting_library.characters.items():
                if other != name and other in s and \
                        (getattr(other_entry, "gender", "") or "").strip() == ("女" if gender == "男" else "男"):
                    has_other_gender = True
                    break
            if not has_other_gender:
                violations.append({
                    "type": "gender_pronoun",
                    "severity": "warning",
                    "description": f"角色「{name}」（{gender}）同句出现相反代词「{wrong}」：{s[:40]}…",
                })
                break  # 每个角色只报一次
    return violations


# ═══════════════════════════════════════════════════════════════
# 实体名门控（正文 vs 设定库：错拼/漂移/凭空发明）
# ═══════════════════════════════════════════════════════════════

def entity_dictionary(setting_library) -> dict:
    """登记名词典：{名字: "类型:规范名"}。覆盖 6 个子库 + 人物别名。"""
    names = {}
    for n, entry in (getattr(setting_library, "characters", None) or {}).items():
        names[n] = f"角色:{n}"
        for alias in (getattr(entry, "aliases", None) or []):
            if alias and len(alias) >= 2:
                names[alias] = f"角色:{n}（别名）"
    for attr, label in (("geography", "地点"), ("factions", "势力"),
                        ("items", "道具"), ("history", "历史"), ("power_system", "战力")):
        for n in (getattr(setting_library, attr, None) or {}):
            names[n] = f"{label}:{n}"
    return names


_CJK_RE = re.compile(r"^[\u4e00-\u9fa5]+$")
# 语法助词尾：窗口以这些字结尾说明是"词素+助词"（吞噬着/暗金瞳孔的），非名字漂移
_PARTICLE_TAIL = set("的之着了后前中内外时其把被让向往与和")


def check_name_variants(content_text: str, setting_library) -> list:
    """近似名检测：正文出现与登记名等长且仅一字之差的片段 → 疑似错拼/名字漂移。
    强信号规则（宁缺毋滥，全部 warning）：
    - 名字 ≥3 字且仅末字不同：出现 1 次即报（夜千尘→夜千辰）
    - 名字 ≥3 字其他单字差异：出现 ≥2 次才报
    - 名字 2 字且仅末字不同：出现 ≥4 次才报（林渊→林源；二字名误报率高）
    误报抑制：窗口须纯汉字、不以助词结尾、非登记名截断、
    非词素家族（前缀被 ≥2 个登记名共用的构词词素，如 吞噬X/上古X/噬魂X）。"""
    names = entity_dictionary(setting_library)
    exact_spans = []
    for name in names:
        start = 0
        while True:
            idx = content_text.find(name, start)
            if idx < 0:
                break
            exact_spans.append((idx, idx + len(name)))
            start = idx + 1

    def in_exact_span(s, e):
        return any(s < be and e > bs for bs, be in exact_spans)

    violations, seen = [], set()
    for name, label in names.items():
        L = len(name)
        if L < 2:
            continue
        for i in range(len(content_text) - L + 1):
            window = content_text[i:i + L]
            if window == name or window in names:
                continue
            if not _CJK_RE.match(window):
                continue  # 含标点/数字（吞噬。）
            if "的" in window or window[-1] in _PARTICLE_TAIL:
                continue  # 描述短语/词素+助词（花白头发的老、吞噬着）
            diffs = [k for k, (a, b) in enumerate(zip(window, name)) if a != b]
            if len(diffs) != 1:
                continue
            if in_exact_span(i, i + L):
                continue
            # 截断抑制：窗口是某登记名的前缀/后缀（吞噬→吞噬纹的截断用法）
            if any(window in other and window != other for other in names):
                continue
            # 词素家族抑制：差异前缀被 ≥2 个登记名共用（吞噬X/上古X 家族），
            # 或本章出现 ≥2 个共享该词素的未登记变体（噬魂雾+噬魂纹并存 → 词素能产，非漂移）
            stem = window[:L - 1] if diffs[0] == L - 1 else window[:diffs[0]] or window[1:]
            if sum(1 for other in names if stem and stem in other and other != name) >= 2:
                continue
            if stem and len({w for w in {content_text[j:j + L] for j in range(len(content_text) - L + 1)}
                             if w != name and w not in names and w.startswith(stem)}) >= 2:
                continue
            cnt = content_text.count(window)
            last_char_differs = diffs[0] == L - 1
            if L >= 3 and last_char_differs and cnt >= 1:
                threshold_met = True
            elif L >= 3 and cnt >= 2:
                threshold_met = True
            elif L == 2 and last_char_differs and cnt >= 4:
                threshold_met = True
            else:
                threshold_met = False
            if threshold_met and window not in seen:
                seen.add(window)
                violations.append({
                    "type": "name_variant",
                    "severity": "warning",
                    "description": f"正文 {cnt} 次出现「{window}」，与登记{label}仅一字之差，疑似错拼或名字漂移",
                })
    return violations


# 对话归属提取：X说道/X冷笑道/X道 → X 为名字候选
# 候选组用懒惰量词：让长的动词链（冷笑道/沉声道）优先被动词组消费，
# 否则贪婪候选会吞掉动词前缀（"林渊冷笑"+"道"）
_ATTR_VERB_RE = re.compile(
    r"([\u4e00-\u9fa5]{2,6}?)"
    r"(?:冷笑道|冷笑着|沉声道|低声道|大笑道|朗声道|开口道|喃喃道|笑道|说着|说道|喊道|问道|骂道|答道|叹道|应道|吼道|喝道|(?<![知难一十])道)")
# 名字候选尾部粘连的副词/助词（"林渊连忙道"→提取"渊连忙"→剥"连忙"）
_ATTR_STRIP_TAIL = set("冷沉低高大小的着了地又再只也不就被和与而并还已没便却连忙急忙赶紧悄悄暗暗")
# 名字候选尾部粘连的称谓（"玄天宗长老说道"→"玄天宗长老"→剥"长老"）
_HONORIFICS = ("长老", "师兄", "师姐", "师弟", "师妹", "师叔", "师伯", "师尊",
               "掌门", "大人", "殿下", "陛下", "公子", "小姐", "姑娘", "执事",
               "导师", "队长", "族老", "宗主", "城主", "队长")
# 高频普通词（对话归属误提取的主要噪声）
_COMMON_WORDS = {
    "时候", "自己", "知道", "已经", "现在", "什么", "这个", "那个", "他们",
    "她们", "它们", "一声", "两人", "众人", "对方", "突然", "忽然", "只见",
    "此时", "此刻", "顿时", "随即", "接着", "然后", "最后", "开始", "终于",
    "声音", "一字", "一句", "旁边", "身后", "面前", "一旁",
}


def extract_attributed_names(content_text: str) -> dict:
    """从对话归属结构提取名字候选及频次：{候选: 次数}。"""
    counts = {}
    for m in _ATTR_VERB_RE.finditer(content_text):
        cand = m.group(1)
        # 剥尾部称谓
        for h in _HONORIFICS:
            if cand.endswith(h) and len(cand) > len(h) + 1:
                cand = cand[:-len(h)]
                break
        # 剥尾部粘连副词
        while len(cand) > 2 and cand[-1] in _ATTR_STRIP_TAIL:
            cand = cand[:-1]
        if len(cand) < 2 or cand in _COMMON_WORDS:
            continue
        counts[cand] = counts.get(cand, 0) + 1
    return counts


def check_unregistered_names(content_text: str, setting_library,
                             min_count: int = 2) -> list:
    """凭空发明检测：对话归属中反复出现的名字候选不在任何登记名（含别名）
    中、也不是任何登记名的子串 → 疑似未登记实体。warning 级。"""
    names = entity_dictionary(setting_library)
    violations = []
    for cand, cnt in extract_attributed_names(content_text).items():
        if cand in names or cand in _COMMON_WORDS:
            continue
        if any(cand in name for name in names):
            continue  # 是某登记名的子串（如全名被截断）
        if cnt >= min_count:
            violations.append({
                "type": "unregistered_name",
                "severity": "warning",
                "description": f"「{cand}」在对话归属中出现 {cnt} 次但不在设定库任何子库中，疑似凭空发明或未登记",
            })
    return violations


# ── 汇总入口 ─────────────────────────────────────────────────

def run_all_checks(content_text: str, outline, setting_library) -> dict:
    """正文落盘前的确定性校验。返回 {passed, violations}。
    error 级违规视为不通过（必须重写），warning 级仅记录。"""
    violations = []
    violations.extend(check_dead_characters(content_text, setting_library))
    violations.extend(check_power_terms(content_text, setting_library))
    violations.extend(check_chapter_refs(content_text))
    violations.extend(check_gender_pronouns(content_text, setting_library))
    violations.extend(check_name_variants(content_text, setting_library))
    violations.extend(check_unregistered_names(content_text, setting_library))
    has_error = any(v.get("severity") == "error" for v in violations)
    return {"passed": not has_error, "violations": violations}
