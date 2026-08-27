"""
评委层 — 多评委盲评 + 可插拔独立模型
=================================================
- 盲评：评审 prompt 不出现"AI 生成"等暴露性表述，避免打分偏差
- 默认同模型多评委（3 档温度取中位数）；配置 JUDGE_API_KEY/JUDGE_BASE_URL/JUDGE_MODEL
  后评委自动切换到独立模型，与写手模型分离
- 提供：签约标准评估（evaluate_signing）、单章质量评估（evaluate_chapter，
  供 50 章长跑的按章漂移曲线）、A/B 盲选（blind_compare）
"""

import statistics
from api_client import APIClient, get_client
from config import (JUDGE_API_KEY, JUDGE_BASE_URL, JUDGE_MODEL,
                    judge_separately_configured)
from utils import print_warning

JUDGE_TEMPERATURES = [0.2, 0.5, 0.8]

# ── 签约评估维度（权重和为 1.0）─────────────────────────────
FANQIE_DIMS = [
    ("opening_hook", "开篇吸引力", 0.25,
     "黄金三章标准：开篇即冲突，前三章内完成「冲突爆发→金手指觉醒→首次打脸/逆袭兑现」的闭环，第一段能否让读者停留"),
    ("pacing", "爽点密度与节奏", 0.20,
     "每章是否有明确爽点（打脸/升级/获得/逆转），节奏是否明快，是否存在大段无效描写或设定堆砌"),
    ("retention", "追读钩子", 0.15,
     "每章结尾是否有吸引追读的钩子，悬念是否持续在线，章节标题是否有点开欲"),
    ("character", "人设与代入感", 0.15,
     "主角目标是否清晰、行动是否主动，配角功能是否明确，读者代入是否顺畅，有无 OOC"),
    ("genre_fit", "题材与定位", 0.10,
     "题材标签是否清晰，受众定位是否明确，是否符合番茄平台当前热门方向"),
    ("prose", "文笔与流畅度", 0.10,
     "语言通顺度、错别字/病句/重复用词情况、是否有人工痕迹明显的模板腔，按网络文学商业化文笔标准评判"),
    ("professionalism", "作品完整度", 0.05,
     "书名点击欲、简介合格度、章节结构完整度"),
]

# ── 单章质量维度（漂移曲线用，同权重平均）──────────────────
CHAPTER_DIMS = [
    ("hook_power", "钩子与爽点", "本章结尾钩子强度、本章爽点（打脸/升级/逆转）是否明确兑现"),
    ("pacing", "节奏", "是否明快，有无大段无效描写/设定堆砌/重复"),
    ("prose", "文笔", "语言是否流畅自然，是否有人工痕迹明显的模板腔、排比堆砌、套话"),
    ("consistency", "一致性", "人物行为、战力表现是否前后一致，有无逻辑漏洞"),
]


def _median_score(results: list, key_path) -> float:
    """从多评委结果中取某数值的中位数；key_path 为取值函数"""
    values = []
    for r in results:
        try:
            v = float(key_path(r))
        except (TypeError, ValueError, KeyError):
            continue
        values.append(v)
    return statistics.median(values) if values else 0.0


class JudgeClient:
    """多评委客户端：同一 system/user prompt，多温度（或独立模型）多次评估后聚合"""

    def __init__(self, n_judges: int = 3):
        if judge_separately_configured():
            self.clients = [APIClient(api_key=JUDGE_API_KEY, model=JUDGE_MODEL,
                                      base_url=JUDGE_BASE_URL or None)]
            self.temperatures = JUDGE_TEMPERATURES[:max(1, n_judges)]
            self.separate = True
        else:
            client = get_client()
            self.clients = [client] * n_judges
            self.temperatures = JUDGE_TEMPERATURES[:n_judges] or [0.3]
            self.separate = False

    def evaluate(self, system_prompt: str, user_prompt: str,
                 max_tokens: int = 8192) -> dict:
        """多评委评估并聚合。评委之间相互独立，并发执行（评委串行是评估阶段的主要耗时）。"""
        from concurrent.futures import ThreadPoolExecutor

        pairs = list(zip(self.clients, self.temperatures))

        def run(pair):
            client, temp = pair
            try:
                return client.chat_with_json_output(
                    system_prompt, user_prompt,
                    temperature=temp, max_tokens=max_tokens)
            except Exception as e:
                print_warning(f"评委调用失败（跳过该评委）: {e}")
                return None

        if len(pairs) <= 1:
            outcomes = [run(pairs[0])] if pairs else []
        else:
            with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
                outcomes = list(pool.map(run, pairs))

        results = [r for r in outcomes if r and "_parse_error" not in r]
        if not results:
            return {"parse_error": True}
        return self._aggregate(results)

    @staticmethod
    def _aggregate(results: list) -> dict:
        """聚合成单一结果：分数取中位数，issues 合并去重，verdict 取分最高者"""
        agg = dict(results[0])  # 保留其余字段（dimensions 等）
        if len(results) == 1:
            return agg

        for key in ("total_score", "score", "weighted_score"):
            if any(key in r for r in results):
                agg[key] = round(_median_score(results, lambda r: r.get(key, 0)), 1)

        # 维度级分数取中位数
        if all("dimensions" in r for r in results):
            dims = {}
            for k in results[0].get("dimensions", {}):
                vals = []
                for r in results:
                    d = r.get("dimensions", {}).get(k)
                    if isinstance(d, dict):
                        try:
                            vals.append(float(d.get("score", 0)))
                        except (TypeError, ValueError):
                            pass
                if vals:
                    comments = [r.get("dimensions", {}).get(k, {}).get("comment", "")
                                for r in results]
                    dims[k] = {"score": round(statistics.median(vals), 1),
                               "comment": max(comments, key=len, default="")[:40]}
            agg["dimensions"] = dims

        # issues 合并去重（按 description 前 30 字）
        seen, merged_issues = set(), []
        for r in results:
            for issue in r.get("issues", []):
                desc = issue.get("description", str(issue))[:30] if isinstance(issue, dict) else str(issue)[:30]
                if desc and desc not in seen:
                    seen.add(desc)
                    merged_issues.append(issue)
        if merged_issues:
            agg["issues"] = merged_issues[:8]

        booleans = [bool(r.get("sign_ready")) for r in results if "sign_ready" in r]
        if booleans:
            agg["sign_ready"] = sum(booleans) * 2 > len(booleans)  # 多数决
        agg["n_judges"] = len(results)
        return agg


# ═══════════════════════════════════════════════════════════════
# 签约标准评估（盲评：不告知评审对象为 AI 生成）
# ═══════════════════════════════════════════════════════════════

def evaluate_signing(judge: JudgeClient, title: str, blurb: str,
                     chapters: list, rough_outline_text: str) -> dict:
    """chapters: [(num, title, clean_text)]，前三章给全文，之后给开头。"""
    dims_desc = "\n".join(
        f"{i + 1}. **{label}**（权重 {w}）：{desc}"
        for i, (k, label, w, desc) in enumerate(FANQIE_DIMS))
    format_dims = ",\n".join(
        f'    "{k}": {{"score": 0到100的整数, "comment": "30字内简评"}}'
        for k, _, _, _ in FANQIE_DIMS)

    chapter_blocks = []
    for num, ctitle, text in chapters:
        body = text if num <= 3 else text[:600] + "…（后略）"
        chapter_blocks.append(f"—— 第{num}章《{ctitle}》（{len(text)}字）——\n{body}")
    chapters_text = "\n\n".join(chapter_blocks)

    system_prompt = f"""你是番茄小说（字节跳动旗下免费网文平台）的签约责编，负责评估作品能否通过签约。
请基于番茄平台的真实签约标准，对一部玄幻新作进行严格评估。

【评估维度】：
{dims_desc}

【评分标准】：
- 90-100：品质过硬，签约无悬念
- 80-89：达到签约线，可放心投递
- 70-79：接近签约线，修改后可投
- 60-69：有较大差距，需重写开篇
- 0-59：未达签约标准

【输出格式】：严格 JSON
{{
  "total_score": 82,
  "dimensions": {{
{format_dims}
  }},
  "sign_ready": true/false,
  "verdict": "一句话结论（达到签约线/接近/未达到及原因）",
  "issues": ["最影响签约的问题1", "问题2"],
  "suggestions": ["具体可执行的修改建议1", "建议2"]
}}
注意：dimensions 的 key 必须使用上述英文 key。comment 每条不超过 30 字，issues/suggestions 各最多 5 条。
直接输出 JSON，不要包含 ```json``` 标记。"""

    user_prompt = f"""请评估以下作品能否在番茄小说签约。

【书名】《{title}》
【简介】
{blurb}

【第一卷粗纲梗概】
{rough_outline_text[:1200]}

【章节正文】
{chapters_text}

请按维度逐一严格评估，直接输出 JSON。"""

    result = judge.evaluate(system_prompt, user_prompt, max_tokens=8192)
    if "parse_error" in result:
        return result

    # 兼容中文标签 key + 计算加权分
    dim_scores = result.get("dimensions", {})
    for k, label, _, _ in FANQIE_DIMS:
        if k not in dim_scores and isinstance(dim_scores.get(label), dict):
            dim_scores[k] = dim_scores.pop(label)
    weighted = 0.0
    for k, _, w, _ in FANQIE_DIMS:
        ds = dim_scores.get(k, {})
        try:
            weighted += float(ds.get("score", 0)) * w
        except (TypeError, ValueError):
            pass
    result["weighted_score"] = round(weighted, 1)
    result["dimensions"] = dim_scores
    return result


# ═══════════════════════════════════════════════════════════════
# 单章质量评估（漂移曲线）
# ═══════════════════════════════════════════════════════════════

def evaluate_chapter(judge: JudgeClient, chapter_number: int, title: str,
                     text: str, previous_summary: str = "") -> dict:
    dims_desc = "\n".join(
        f"{i + 1}. **{label}**：{desc}"
        for i, (k, label, desc) in enumerate(CHAPTER_DIMS))
    format_dims = ",\n".join(
        f'    "{k}": {{"score": 0到100的整数, "comment": "20字内简评"}}'
        for k, _, _ in CHAPTER_DIMS)
    prev_block = f"\n【前文摘要】\n{previous_summary[:800]}\n" if previous_summary else ""

    system_prompt = f"""你是番茄小说的签约责编，正在追读一部连载作品，评估单个章节的成稿质量。

【评估维度】：
{dims_desc}

【输出格式】：严格 JSON
{{
  "total_score": 80,
  "dimensions": {{
{format_dims}
  }},
  "issues": ["最突出的问题1", "问题2"]
}}
评分标准：90-100=精品章节，80-89=合格，70-79=有明显问题，<70=不合格。
直接输出 JSON，不要包含 ```json``` 标记。"""

    user_prompt = f"""请评估第 {chapter_number} 章《{title}》（{len(text)}字）的成稿质量。
{prev_block}
【章节正文】
{text if len(text) <= 5000 else text[:5000] + "…（后略）"}

请按维度逐一严格评估，直接输出 JSON。"""

    result = judge.evaluate(system_prompt, user_prompt, max_tokens=4096)
    if "parse_error" in result:
        return result
    dim_scores = result.get("dimensions", {})
    for k, label, _ in CHAPTER_DIMS:
        if k not in dim_scores and isinstance(dim_scores.get(label), dict):
            dim_scores[k] = dim_scores.pop(label)
    scores = []
    for k, _, _ in CHAPTER_DIMS:
        ds = dim_scores.get(k, {})
        try:
            scores.append(float(ds.get("score", 0)))
        except (TypeError, ValueError):
            pass
    if scores:
        result["total_score"] = round(sum(scores) / len(scores), 1)
    result["dimensions"] = dim_scores
    return result


# ═══════════════════════════════════════════════════════════════
# A/B 盲选（prompt 改进对照实验用）
# ═══════════════════════════════════════════════════════════════

def blind_compare(judge: JudgeClient, text_a: str, text_b: str,
                  criteria: str = "网文成稿质量（文笔自然度、爽点、钩子、节奏）") -> dict:
    """盲选两版正文哪版更好。A/B 顺序随机轮换一次以消除位置偏差。"""
    system_prompt = f"""你是番茄小说的签约责编。下面有同一章节的两个版本 A 和 B，请盲选出 {criteria} 更好的一版。

【输出格式】：严格 JSON
{{"winner": "A 或 B", "confidence": "high/medium/low", "reason": "50字内理由"}}"""
    user_prompt = f"""【版本A】\n{text_a[:5000]}\n\n【版本B】\n{text_b[:5000]}\n\n请输出盲选 JSON。"""

    result = judge.evaluate(system_prompt, user_prompt, max_tokens=2048)
    if "parse_error" in result:
        return result
    winner = str(result.get("winner", "")).strip().upper()
    if winner not in ("A", "B"):
        result["winner"] = "tie"
    return result
