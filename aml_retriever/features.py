"""AML Retriever — 确定性特征提取（零依赖）。

提供 CJK n-gram、拉丁词、数字、日期、引用短语、实体式 token 的确定性提取。
所有函数均为纯函数，结果可复现，不依赖任何外部资源或模型。
"""
from __future__ import annotations

import re

# 中日韩统一表意文字（含扩展 A）与兼容表意文字
_CJK_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]+")
_LAT_RE = re.compile(r"[a-z0-9]+")
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
_DATE_RE = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}[-/]\d{1,2}[-/]\d{4}"
    r"|\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?"
)
_PHRASE_RE = re.compile(r"[\u201c\"]([^\u201d\"]{1,64})[\u201d\"]")
# 实体式拉丁 token：大写开头的词、全大写缩写、带连字符的专名
_ENTITY_LAT_RE = re.compile(r"\b(?:[A-Z][a-zA-Z0-9]{1,}|[A-Z]{2,})\b")

# 检索价值极低的高频停用词（只用于查询侧降噪，索引侧不丢弃）
_STOP = {
    "the", "a", "an", "of", "to", "and", "or", "in", "on", "at", "for", "is", "are",
    "was", "were", "be", "been", "it", "this", "that", "with", "as", "by", "from",
    "what", "which", "who", "when", "where", "how", "did", "do", "does", "his",
    "her", "their", "they", "he", "she", "you", "i", "we", "best", "matches",
    "answer", "question", "memory",
    "\u7684", "\u4e86", "\u662f", "\u5728", "\u548c", "\u6709", "\u4ec0\u4e48",
}

# 时间意图标记：出现这些词，说明用户问的是「当前状态」而非「历史上说过什么」。
# 此时同一属性的多条记录里，最新的那条才是答案，需要显著抬高新近度权重。
_TEMPORAL_CN = (
    "现在", "目前", "当前", "如今", "最近", "最新", "眼下", "这个月", "这周",
    "今天", "近期", "改成", "换成", "还是", "已经",
)
_TEMPORAL_EN = (
    "now", "current", "currently", "latest", "recent", "recently", "today",
    "these days", "nowadays", "at present", "up to date", "still",
)

# 通用更新语义。只描述“旧状态被新状态替换”的语言现象，不包含评测实体、
# 数值或专有名词；用于可选的 supersession 防误判保护。
_UPDATE_CN = (
    "更新为", "改为", "改成", "换为", "换成", "调整为", "变更为", "转为",
    "已上调", "已下调", "上调为", "下调为", "最新口径", "新口径",
    "作废", "不再", "取代", "替换为", "迁移到", "搬到",
)
_UPDATE_EN = (
    "updated to", "changed to", "switched to", "revised to", "is now",
    "no longer", "replaced by", "supersedes", "deprecated", "new value",
    "effective from", "moved to", "migrated to",
)

# 偏好查询意图与第一人称直接陈述。后者比单纯 role=user 更严格，避免把
# “某某更喜欢……”这类用户转述误当成用户本人的偏好。
_PREFERENCE_CN = (
    "喜欢", "偏好", "首选", "最爱", "爱用", "常用", "习惯", "更愿意", "合我",
)
_PREFERENCE_EN = (
    "prefer", "preference", "favorite", "favourite", "go-to", "usually use",
    "tend to use", "like to use",
)
_NUMERIC_INTENT_CN = (
    "多少", "几个", "几号", "几点", "金额", "预算", "价格", "费用", "数量",
    "编号", "号码", "版本", "余额", "额度", "比例", "百分比",
)
_NUMERIC_INTENT_EN = (
    "how much", "how many", "amount", "budget", "price", "cost", "number",
    "version", "balance", "quota", "percentage", "ratio",
)
_DATE_INTENT_CN = (
    "什么时候", "何时", "哪天", "哪一天", "日期", "几号", "年月日",
)
_DATE_INTENT_EN = (
    "when", "what date", "which date", "release date", "start date", "end date",
)
_DIRECT_PREFERENCE_CN_RE = re.compile(
    r"(?:我|本人|我们)[^。！？!?]{0,16}(?:喜欢|偏好|首选|最爱|爱用|常用|习惯|更愿意)"
)
_DIRECT_PREFERENCE_EN_RE = re.compile(
    r"\b(?:i|we)\s+(?:(?:really|usually|generally)\s+)?"
    r"(?:prefer|like|favor|favour|use|tend\s+to\s+use)\b"
    r"|\b(?:my|our)\s+(?:favorite|favourite|preferred|go-to)\b",
    re.IGNORECASE,
)


def has_temporal_intent(query: str) -> bool:
    """判断查询是否在问「当前状态」。纯确定性规则，无模型依赖。"""
    if not query:
        return False
    lowered = query.lower()
    if any(marker in query for marker in _TEMPORAL_CN):
        return True
    return any(marker in lowered for marker in _TEMPORAL_EN)


def has_update_cue(text: str) -> bool:
    """判断陈述是否显式表达状态替换或旧值失效。"""
    if not text:
        return False
    lowered = text.lower()
    if any(marker in text for marker in _UPDATE_CN):
        return True
    return any(marker in lowered for marker in _UPDATE_EN)


def has_preference_intent(text: str) -> bool:
    """判断查询是否在询问偏好或习惯。"""
    if not text:
        return False
    lowered = text.lower()
    if any(marker in text for marker in _PREFERENCE_CN):
        return True
    return any(marker in lowered for marker in _PREFERENCE_EN)


def has_direct_preference_statement(text: str) -> bool:
    """判断文本是否为第一人称偏好陈述，而非对第三人的转述。"""
    if not text:
        return False
    return bool(
        _DIRECT_PREFERENCE_CN_RE.search(text)
        or _DIRECT_PREFERENCE_EN_RE.search(text)
    )


def has_numeric_value_intent(text: str) -> bool:
    """判断查询是否明确索要金额、数量、编号或版本类数值状态。"""
    if not text:
        return False
    lowered = text.lower()
    if any(marker in text for marker in _NUMERIC_INTENT_CN):
        return True
    return any(marker in lowered for marker in _NUMERIC_INTENT_EN)


def has_date_value_intent(text: str) -> bool:
    """判断查询是否明确索要某个日期或发生时间。"""
    if not text:
        return False
    lowered = text.lower()
    if any(marker in text for marker in _DATE_INTENT_CN):
        return True
    return any(marker in lowered for marker in _DATE_INTENT_EN)


def cjk_ngrams(text: str, n: int) -> list[str]:
    out: list[str] = []
    for match in _CJK_RE.finditer(text or ""):
        run = match.group(0)
        if n == 1:
            out.extend(list(run))
        else:
            for i in range(len(run) - n + 1):
                out.append(run[i : i + n])
    return out


def tokenize(text: str, max_ngram: int = 3) -> list[str]:
    """索引侧分词：CJK 1~3-gram + 拉丁词 + 数字 + 日期，统一小写。"""
    text = (text or "").lower()
    tokens: list[str] = []
    for n in range(1, max(1, max_ngram) + 1):
        tokens.extend(cjk_ngrams(text, n))
    tokens.extend(_LAT_RE.findall(text))
    tokens.extend(t.replace(",", "") for t in _NUM_RE.findall(text))
    tokens.extend(_DATE_RE.findall(text))
    return tokens


def index_text(text: str) -> str:
    """生成写入 FTS5 的索引串（去重以压缩体积，检索语义不变）。"""
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokenize(text):
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return " ".join(out)


def query_tokens(text: str, cap: int = 64) -> list[str]:
    """查询侧 token：去停用词、去重，并按"长者优先"确定性截断。

    截断是为了界定 FTS5 OR 查询的最坏代价；长 token（trigram / 日期 / 长词）
    区分度更高，优先保留。同长度按字典序，保证完全确定性。
    """
    raw = tokenize(text)
    uniq = {t for t in raw if t and t not in _STOP}
    ordered = sorted(uniq, key=lambda t: (-len(t), t))
    return ordered[: max(1, cap)]


def extract_numbers(text: str) -> list[str]:
    return [t.replace(",", "") for t in _NUM_RE.findall(text or "")]


def extract_non_date_numbers(text: str) -> list[str]:
    """提取日期表达式之外的数字，供答案类型保护使用。"""
    return extract_numbers(_DATE_RE.sub(" ", text or ""))


def extract_dates(text: str) -> list[str]:
    return _DATE_RE.findall(text or "")


def extract_phrases(text: str) -> list[str]:
    """提取被引号包裹的精确短语（中文 \u201c \u201d 或英文 " "）。"""
    return [m.group(1) for m in _PHRASE_RE.finditer(text or "")]


def extract_entities(text: str) -> list[str]:
    """确定性"实体式" token：拉丁专名/缩写 + CJK 2~4gram（作为中文实体近似）。"""
    out: list[str] = []
    out.extend(m.group(0) for m in _ENTITY_LAT_RE.finditer(text or ""))
    for n in (2, 3, 4):
        out.extend(cjk_ngrams(text or "", n))
    seen: set[str] = set()
    uniq: list[str] = []
    for tok in out:
        low = tok.lower()
        if low not in seen:
            seen.add(low)
            uniq.append(tok)
    return uniq


def normalize(text: str) -> str:
    return (text or "").lower()


__all__ = [
    "tokenize",
    "index_text",
    "query_tokens",
    "cjk_ngrams",
    "extract_numbers",
    "extract_non_date_numbers",
    "extract_dates",
    "extract_phrases",
    "extract_entities",
    "normalize",
    "has_temporal_intent",
    "has_update_cue",
    "has_preference_intent",
    "has_direct_preference_statement",
    "has_numeric_value_intent",
    "has_date_value_intent",
]
