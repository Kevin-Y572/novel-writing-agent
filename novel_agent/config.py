"""
全局配置 — API、路径、常量
"""

import os


def _load_local_env() -> dict:
    """从模块同级的 .env 文件读取 KEY=VALUE（密钥不入源码，优先级低于环境变量）"""
    env = {}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    except OSError:
        pass
    return env


_LOCAL_ENV = _load_local_env()


def _get(name: str, default: str = "") -> str:
    """环境变量 > 本地 .env > 默认值"""
    return os.getenv(name) or _LOCAL_ENV.get(name, default)


# ── DeepSeek API ─────────────────────────────────────────────
# 密钥只从环境变量 DEEPSEEK_API_KEY 或同目录 .env 文件读取，不写入源码
DEEPSEEK_API_KEY = _get("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# 主力模型；可用环境变量 DEEPSEEK_MODEL 临时覆盖（如切回 deepseek-v4-pro 对比评测）
DEEPSEEK_MODEL = _get("DEEPSEEK_MODEL", "deepseek-v4-flash")

# ── 评委模型（可选）─────────────────────────────────────────
# 配置后签约评估/harness 评审走独立模型，避免"自己给自己打分"；留空则复用主力模型
JUDGE_API_KEY = _get("JUDGE_API_KEY", "")
JUDGE_BASE_URL = _get("JUDGE_BASE_URL", "")
JUDGE_MODEL = _get("JUDGE_MODEL", "")


def api_key_configured() -> bool:
    return bool(DEEPSEEK_API_KEY)


def judge_separately_configured() -> bool:
    """评委是否配置了独立模型"""
    return bool(JUDGE_MODEL and JUDGE_API_KEY)

# ── 路径 ─────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", "projects")

# ── 默认值 ───────────────────────────────────────────────────
DEFAULT_WORDS_PER_CHAPTER = 3000
DEFAULT_CHAPTERS_PER_VOLUME = 50
MAX_SETTING_ENTRIES_PER_TYPE = 50  # 每种设定库最大条目数

# ── 文风预设 ─────────────────────────────────────────────────
STYLE_PRESETS = {
    "番茄爆款": "节奏明快、爽点密集、每章有钩子、对话生动、网络感强、适当玩梗",
    "科幻硬核": "逻辑严谨、设定详实、语言冷静克制、技术细节丰富、宏大叙事",
    "轻松日常": "幽默风趣、互动自然、日常中见温情、轻松节奏、生活气息浓厚",
    "热血战斗": "燃点密集、战斗描写细致、情绪张力强、成长感明显、主角不服输",
    "悬疑烧脑": "伏笔密集、反转频繁、线索层层递进、气氛营造强、逻辑闭环",
}

# ── 反 AI 腔写作禁则（注入章节写作系统提示词）────────────────
ANTI_AI_STYLE_RULES = """【反AI腔硬规则（违反视为写作质量不合格）】：
- 禁止三句及以上排比堆砌；一句话说清的事不用"不是…而是…"反复对仗
- 禁止结尾升华总结（如"这一刻他明白了…""他知道，属于他的时代才刚刚开始"），章节用动作或悬念收束
- 禁止套话模板："仿佛在诉说""空气中弥漫着""时间仿佛静止""眼中闪过一丝不易察觉的""嘴角勾起一抹弧度"
- 对话写人话：口语化、可打断、可答非所问，禁止人人腔调相同的长段书面独白
- 句长要有错落：长句后接短句；连续三句等长即为病
- 描写给具体的：写"他攥紧的指节发白"，不写"他的内心充满了愤怒与不甘\""""

# ── 网感程度 ─────────────────────────────────────────────────
INTERNET_SLANG_LEVELS = {
    "无": "不使用任何网络热梗和流行语，保持传统文学语言风格",
    "低": "偶尔使用广为人知的网络用语，保持克制，不影响阅读流畅性",
    "中": "适度使用网络热梗和流行语，增强代入感和时代感，但不喧宾夺主",
    "高": "大量融入最新网络热梗、流行语和互联网文化元素，行文风格贴近社交媒体语境",
}

# ── 叙事人称 ─────────────────────────────────────────────────
NARRATIVE_PERSONS = ["第一人称", "第三人称", "第三人称有限视角（跟随主角）"]

# ── 故事分类 ─────────────────────────────────────────────────
GENRES = ["玄幻", "仙侠", "科幻", "历史", "都市", "悬疑", "游戏", "末世"]
