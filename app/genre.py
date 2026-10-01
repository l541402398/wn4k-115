"""影片类型（genre）推断。

背景：蜗牛站点**没有提供类型字段**——详情页标签只有 评分/年份/地区/分类/清晰度，
且 `/vodshow/1--动作--/` 这类类型筛选 URL 返回 404。所以「动作/喜剧」这一层
由本地关键词规则推断。

规则文件：data/genre_rules.json
- 首次运行会写入一份默认规则；
- 之后该文件就是唯一依据，你改了立即生效（可在界面里编辑，也可直接改文件）；
- 想恢复默认：删掉该文件重启，或调 reset_genre_rules()。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .config import DATA_DIR, ensure_dirs

GENRE_FALLBACK = "其他"
RULES_PATH = DATA_DIR / "genre_rules.json"

# 默认规则：键是类型名，值是用于匹配的关键词。
# 词表刻意偏保守——宁可归入「其他」，也不要乱贴类型。
DEFAULT_GENRE_RULES: dict[str, list[str]] = {
    "动作": [
        "动作", "格斗", "武打", "武术", "枪战", "爆炸", "特工", "间谍", "搏击",
        "追杀", "复仇", "刺杀", "黑帮", "警匪", "飙车", "劫案", "打斗", "战警",
    ],
    "科幻": [
        "科幻", "太空", "宇宙", "外星", "飞船", "机器人", "机械人", "人工智能",
        "时空", "穿越", "平行世界", "末日", "赛博", "克隆", "变异", "星际",
    ],
    "喜剧": ["喜剧", "搞笑", "幽默", "爆笑", "荒诞", "闹剧", "欢乐"],
    "爱情": ["爱情", "恋爱", "浪漫", "初恋", "暗恋", "情侣", "婚外", "情感"],
    "恐怖": ["恐怖", "惊悚", "灵异", "鬼", "凶宅", "诅咒", "血腥", "丧尸", "僵尸"],
    "悬疑": ["悬疑", "推理", "侦探", "谜案", "谋杀", "凶手", "犯罪心理", "真相"],
    "犯罪": ["犯罪", "罪案", "贩毒", "走私", "盗窃", "绑架", "诈骗", "越狱", "黑社会"],
    "剧情": ["剧情", "人生", "家庭", "亲情", "成长", "命运", "励志"],
    "战争": ["战争", "战役", "二战", "士兵", "前线", "抗战", "军事"],
    "奇幻": ["奇幻", "魔法", "巫师", "神话", "精灵", "龙", "诅咒之力", "异世界"],
    "冒险": ["冒险", "探险", "寻宝", "荒岛", "求生", "旅程", "远征"],
    "动画": ["动画", "动漫", "剧场版", "番剧", "OVA", "手绘"],
    "纪录": ["纪录", "纪实", "真实事件", "访谈", "自然", "野生动物"],
    "历史": ["历史", "古代", "王朝", "皇帝", "宫廷", "民国", "史诗"],
    "音乐": ["音乐", "演唱会", "乐队", "歌手", "歌曲", "交响", "音乐剧"],
    "家庭": ["家庭", "亲子", "儿童", "父女", "母子", "一家人"],
    "运动": ["运动", "体育", "足球", "篮球", "拳击", "赛车", "比赛"],
}

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_segment(text: str, *, maxlen: int = 80) -> str:
    """把一段文本变成合法的目录名（115 不允许 <>，且不能有路径分隔符）。"""
    value = _INVALID_CHARS.sub("", str(text or ""))
    value = re.sub(r"\s+", " ", value).strip()
    # Windows/115 都不喜欢结尾的点和空格
    value = value.strip(". ")
    return value[:maxlen].strip(". ")


def load_genre_rules(*, create_if_missing: bool = True) -> dict[str, list[str]]:
    """读取规则；文件不存在时写入默认规则并返回。"""
    ensure_dirs()
    if not RULES_PATH.exists():
        if create_if_missing:
            save_genre_rules(DEFAULT_GENRE_RULES)
        return {k: list(v) for k, v in DEFAULT_GENRE_RULES.items()}
    try:
        raw = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {k: list(v) for k, v in DEFAULT_GENRE_RULES.items()}
    rules: dict[str, list[str]] = {}
    if isinstance(raw, dict):
        for genre, words in raw.items():
            name = str(genre).strip()
            if not name:
                continue
            if isinstance(words, str):
                words = [w for w in re.split(r"[,，\s]+", words) if w]
            if isinstance(words, list):
                rules[name] = [str(w).strip() for w in words if str(w).strip()]
    return rules or {k: list(v) for k, v in DEFAULT_GENRE_RULES.items()}


def save_genre_rules(rules: dict[str, Any]) -> dict[str, list[str]]:
    cleaned: dict[str, list[str]] = {}
    for genre, words in (rules or {}).items():
        name = str(genre).strip()
        if not name:
            continue
        if isinstance(words, str):
            words = [w for w in re.split(r"[,，\s]+", words) if w]
        if not isinstance(words, list):
            continue
        cleaned[name] = [str(w).strip() for w in words if str(w).strip()]
    ensure_dirs()
    RULES_PATH.write_text(
        json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return cleaned


def reset_genre_rules() -> dict[str, list[str]]:
    return save_genre_rules(DEFAULT_GENRE_RULES)


def infer_genre(
    *,
    title: str = "",
    desc: str = "",
    extra: str = "",
    rules: dict[str, list[str]] | None = None,
    fallback: str = GENRE_FALLBACK,
) -> str:
    """按关键词打分推断类型。

    片名命中权重高（3 分），简介次之（1 分），其它信息（标签/备注）最低（1 分）。
    没有任何命中就返回 fallback —— 不猜。
    """
    rules = rules or load_genre_rules()
    title_l = (title or "").lower()
    desc_l = (desc or "").lower()
    extra_l = (extra or "").lower()

    best_genre = fallback
    best_score = 0
    for genre, words in rules.items():
        score = 0
        for word in words:
            w = (word or "").lower()
            if not w:
                continue
            if w in title_l:
                score += 3 * title_l.count(w)
            if w in desc_l:
                score += 1 * min(desc_l.count(w), 3)
            if w in extra_l:
                score += 1 * min(extra_l.count(w), 2)
        if score > best_score:
            best_score = score
            best_genre = genre
    return best_genre


def render_path_template(template: str, context: dict[str, str]) -> str:
    """用上下文渲染目录模板，逐段清洗。

    模板形如 "{category}/{genre}/{year}/{title}"。
    未识别的占位符保留为空、空段自动跳过，避免出现 "//" 或 "{xxx}" 这种目录名。
    """

    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        return str(context.get(key, "") or "")

    rendered = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", replace, template or "")
    parts: list[str] = []
    for raw in rendered.split("/"):
        seg = sanitize_segment(raw)
        if seg:
            parts.append(seg)
    return "/".join(parts)