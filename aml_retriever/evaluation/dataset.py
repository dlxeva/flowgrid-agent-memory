"""确定性合成评测数据集。

设计约束
--------
* **纯合成**：人名、地名、项目名全部虚构，不含任何真实个人数据或外部语料。
* **确定性**：同一 ``(seed, scale, difficulty, suite)`` 必然产出逐字节相同的数据集。
* **非平凡**：每个用户有一个固定"主角"，gold 事实被同主角、同句式、
  仅换属性名的**硬干扰项**包围（工位编号 / 门禁卡编号 / 内线号码 …）。
  仅靠"命中人名"无法定位答案，必须区分属性语义。

难度档
------
``plain``       查询与 gold 存在词面重叠（"工位编号是多少"）。词法基线可解。
``paraphrase``  查询改写，**刻意避开 gold 的关键 token**（"坐在哪个位置办公"）。
                纯词法检索会明显掉点，用于暴露真实上限。
``mixed``       两者各半（默认）。

五类查询
--------
``single_hop``        单条消息即可支撑答案。
``multi_session``     答案分散在同一用户的两个不同 session。
``temporal``          同一属性被多次覆写，只有最新一条是 gold。
``knowledge_update``  与 temporal 同构，额外记录旧值消息为 ``distractors``。
``absent``            答案从未出现，gold 为空，用于观察系统是否硬凑证据。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, asdict

# ---------------------------------------------------------------- 合成词表
PERSONS = ["林岚", "赵启明", "沈砚", "苏怀瑾", "郑北屿", "叶知秋",
           "顾长风", "白薇", "陆之涣", "温宜年", "祁昭", "邵清和"]
PROJECTS = ["猎户座", "银鲟", "青柏", "回声塔", "north-quill", "Kestrel-7",
            "赤羽", "moonshard", "长庚", "Vireo"]
CITIES = ["青岚市", "白鹭湾", "沉星港", "屿川", "北砚城", "落枫渡"]
TOOLS = ["Grellet", "潮汐板", "Norsk-CLI", "砚台编辑器", "Palewind", "轴心面板"]
TASKS = ["批注归档", "夜间构建", "指标回灌", "配置对齐", "样本抽检"]
ROLES = ["接口评审", "灰度放量", "数据对齐", "容量规划", "回归验收"]
FOODS = ["咸口豆花", "冷萃乌龙", "海苔烧饼", "酸角糕", "黑麦司康"]
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五"]

# 与"工位编号"极易混淆的同主角属性，用于制造硬干扰项
CONFUSABLE_IDS = ["门禁卡编号", "内线电话", "储物柜编号", "工牌号", "车位编号"]

CHITCHAT = [
    "今天的会议室又被占了，改到下午再说吧。",
    "刚才那份文档我扫了一遍，先不急着定。",
    "网络有点抖，稍等我重连一下。",
    "这个季度的日程排得比上个季度松一些。",
    "先记一下，回头再补充细节。",
    "我这边看到的界面和你描述的不太一样。",
    "这周的例会挪到明天上午了。",
    "刚把上次遗留的两个条目关掉了。",
    "刚才那条消息发错窗口了，忽略即可。",
    "等对方回复之后我们再往下推进。",
]

DIFFICULTIES = ("plain", "paraphrase", "mixed")
SUITES = ("classic", "v11")


@dataclass
class Query:
    qid: str
    user_id: str
    kind: str
    difficulty: str
    text: str
    gold: list[str] = field(default_factory=list)
    distractors: list[str] = field(default_factory=list)


@dataclass
class Dataset:
    seed: int
    scale: str
    difficulty: str
    suite: str
    users: list[str]
    sessions: list[dict]
    queries: list[Query]

    @property
    def message_count(self) -> int:
        return sum(len(s["messages"]) for s in self.sessions)

    @property
    def scored_queries(self) -> list[Query]:
        return [q for q in self.queries if q.gold]

    def kind_counts(self) -> dict:
        out: dict[str, int] = {}
        for q in self.queries:
            out[q.kind] = out.get(q.kind, 0) + 1
        return dict(sorted(out.items()))

    def difficulty_counts(self) -> dict:
        out: dict[str, int] = {}
        for q in self.queries:
            out[q.difficulty] = out.get(q.difficulty, 0) + 1
        return dict(sorted(out.items()))

    def to_dict(self) -> dict:
        return {
            "seed": self.seed,
            "scale": self.scale,
            "difficulty": self.difficulty,
            "suite": self.suite,
            "users": len(self.users),
            "sessions": len(self.sessions),
            "messages": self.message_count,
            "queries": len(self.queries),
            "scored_queries": len(self.scored_queries),
            "queries_by_kind": self.kind_counts(),
            "queries_by_difficulty": self.difficulty_counts(),
        }

    def dump(self) -> dict:
        return {
            "meta": self.to_dict(),
            "sessions": self.sessions,
            "queries": [asdict(q) for q in self.queries],
        }


SCALES = {
    # scale: (users, sessions_per_user, messages_per_session)
    "smoke": (4, 3, 16),
    "small": (16, 3, 28),
    "medium": (48, 3, 36),
    "large": (120, 4, 40),
}

_BASE_TS = 1_760_000_000_000  # 固定基准时间戳(ms)
_MINUTE = 60_000


def _msg(role: str, content: str, ts: int) -> dict:
    return {"role": role, "content": content, "timestamp": ts}


def make_dataset(seed: int = 20260806, scale: str = "small",
                 difficulty: str = "mixed", suite: str = "classic") -> Dataset:
    """生成确定性合成数据集。"""
    if scale not in SCALES:
        raise ValueError(f"unknown scale: {scale}; expected one of {sorted(SCALES)}")
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"unknown difficulty: {difficulty}; expected one of {DIFFICULTIES}")
    if suite not in SUITES:
        raise ValueError(f"unknown suite: {suite}; expected one of {SUITES}")

    n_users, n_sessions, n_messages = SCALES[scale]
    rng = random.Random(seed)

    users = [f"u{idx:04d}" for idx in range(n_users)]
    sessions: list[dict] = []
    queries: list[Query] = []

    for u_idx, user_id in enumerate(users):
        # 该用户的固定主角与实体，跨 session 稳定
        hero = PERSONS[(u_idx + seed) % len(PERSONS)]
        project = PROJECTS[(u_idx * 3 + seed) % len(PROJECTS)]
        city = CITIES[(u_idx * 5 + seed) % len(CITIES)]
        # 每个用户按 index 决定本条查询走 plain 还是 paraphrase
        if difficulty == "mixed":
            level = "plain" if u_idx % 2 == 0 else "paraphrase"
        else:
            level = difficulty

        # --- 语料骨架：闲聊 + 硬干扰项 -----------------------------------
        user_sessions: list[dict] = []
        for s_idx in range(n_sessions):
            session_id = f"{user_id}-s{s_idx}"
            base = _BASE_TS + (u_idx * 10_000 + s_idx * 3_000) * _MINUTE
            msgs: list[dict] = []
            for m_idx in range(n_messages):
                ts = base + m_idx * 7 * _MINUTE
                if m_idx % 3 == 1:
                    # 硬干扰项：同主角、同句式，只换属性名 → 人名无法消歧
                    attr = CONFUSABLE_IDS[(m_idx + s_idx) % len(CONFUSABLE_IDS)]
                    text = f"{hero}的{attr}是 {4000 + (m_idx * 41 + s_idx * 7 + u_idx) % 900}。"
                elif m_idx % 3 == 2:
                    # 中干扰项：同句式换主角
                    other = PERSONS[(u_idx + m_idx + 1) % len(PERSONS)]
                    other_proj = PROJECTS[(u_idx + m_idx + 2) % len(PROJECTS)]
                    text = rng.choice([
                        f"{other}的工位编号是 {2000 + (m_idx * 37 + u_idx) % 900}。",
                        f"{other_proj} 的评审定在 {WEEKDAYS[m_idx % 5]}下午三点。",
                        f"{other}说他更喜欢用 {rng.choice(TOOLS)} 来做{rng.choice(TASKS)}。",
                        f"{other_proj} 本季度的预算是 {(m_idx + 3) * 1700} 元。",
                    ])
                else:
                    text = rng.choice(CHITCHAT)
                msgs.append(_msg("user" if m_idx % 2 == 0 else "assistant", text, ts))
            user_sessions.append({"user_id": user_id, "session_id": session_id,
                                  "messages": msgs, "_base": base})

        def plant(s_idx: int, m_idx: int, content: str, role: str = "user") -> str:
            sess = user_sessions[s_idx]
            sess["messages"][m_idx] = _msg(role, content, sess["_base"] + m_idx * 7 * _MINUTE)
            return f"{sess['session_id']}#{m_idx}"

        last_s = n_sessions - 1

        # --- 1. single_hop：工位编号 ------------------------------------
        desk = 3000 + (u_idx * 13) % 900
        gid = plant(0, 2, f"{hero}的工位编号是 {desk}，在{city}办公区三层靠窗。")
        q_plain = f"{hero}的工位编号是多少？"
        q_para = f"{hero}平时坐在哪儿办公？"
        queries.append(Query(f"q{u_idx:04d}-single", user_id, "single_hop", level,
                             q_plain if level == "plain" else q_para, [gid]))

        # --- 2. multi_session：答案跨两个 session -----------------------
        role_word = ROLES[u_idx % len(ROLES)]
        gid_a = plant(0, 5, f"{project} 的负责人换成了{hero}。")
        gid_b = plant(min(1, last_s), 4,
                      f"{project} 的{role_word}窗口固定在每{WEEKDAYS[u_idx % 5]}上午。")
        q_plain = f"{project} 现在谁负责，{role_word}窗口是什么时候？"
        q_para = f"{project} 那边现在归谁管，什么时候能过一轮？"
        queries.append(Query(f"q{u_idx:04d}-multi", user_id, "multi_session", level,
                             q_plain if level == "plain" else q_para, [gid_a, gid_b]))

        # --- 3. temporal：属性被覆写，只有最新一条为 gold ---------------
        old_food = FOODS[u_idx % len(FOODS)]
        new_food = FOODS[(u_idx + 2) % len(FOODS)]
        stale = plant(0, 8, f"{hero}说他早餐一直吃{old_food}。")
        gid_latest = plant(last_s, n_messages - 3,
                           f"{hero}说他从这个月起早餐改成了{new_food}，不再吃{old_food}。")
        q_plain = f"{hero}现在早餐吃什么？"
        q_para = f"{hero}最近一顿是怎么解决的？"
        queries.append(Query(f"q{u_idx:04d}-temporal", user_id, "temporal", level,
                             q_plain if level == "plain" else q_para, [gid_latest], [stale]))

        # --- 4. knowledge_update：预算被覆写 ----------------------------
        old_budget = (u_idx + 1) * 2300
        new_budget = (u_idx + 1) * 4100
        stale_b = plant(0, 11, f"{project} 本季度的预算是 {old_budget} 元。")
        gid_b2 = plant(min(2, last_s), n_messages - 6,
                       f"{project} 的预算已上调，最新口径是 {new_budget} 元，旧的 {old_budget} 元作废。")
        q_plain = f"{project} 最新的预算是多少？"
        q_para = f"{project} 现在还剩多少钱可以花？"
        queries.append(Query(f"q{u_idx:04d}-update", user_id, "knowledge_update", level,
                             q_plain if level == "plain" else q_para, [gid_b2], [stale_b]))

        # --- 5. absent：答案从未出现 ------------------------------------
        queries.append(Query(f"q{u_idx:04d}-absent", user_id, "absent", level,
                             f"{hero}的护照号码是多少？", [], []))

        if suite == "v11":
            # 独立 v1.1 代理集：不改写经典查询，只追加两个明确的失败模式。
            # 这些语句和实体均为合成数据；规则不可读取 qid/gold/distractor。
            probe_session_id = f"{user_id}-v11"
            probe_base = _BASE_TS + (u_idx * 10_000 + n_sessions * 3_000 + 1_000) * _MINUTE
            old_budget = (u_idx + 7) * 1900
            new_budget = (u_idx + 7) * 3700
            direct_tool = TOOLS[(u_idx + seed) % len(TOOLS)]
            suggested_tool = TOOLS[(u_idx + seed + 1) % len(TOOLS)]
            third_party_tool = TOOLS[(u_idx + seed + 2) % len(TOOLS)]
            probe_messages = [
                _msg("user", f"{project}预算口径是 {old_budget} 元。", probe_base),
                _msg("assistant", "我先把这一项记到待办里。", probe_base + 7 * _MINUTE),
                _msg(
                    "user",
                    f"{project}预算口径已更新为 {new_budget} 元，旧的 {old_budget} 元作废。",
                    probe_base + 14 * _MINUTE,
                ),
                _msg(
                    "assistant",
                    f"如果让我建议，我会用{suggested_tool}做样本抽检。",
                    probe_base + 21 * _MINUTE,
                ),
                _msg(
                    "user",
                    f"我做样本抽检时更喜欢用{direct_tool}，这是我的常用工具。",
                    probe_base + 28 * _MINUTE,
                ),
                _msg(
                    "user",
                    f"{hero}做样本抽检时更喜欢用{third_party_tool}。",
                    probe_base + 35 * _MINUTE,
                ),
                _msg(
                    "user",
                    f"{project}预算口径已更新进说明文档，数值保持不变。",
                    probe_base + 42 * _MINUTE,
                ),
                _msg("assistant", "说明文档已经归档。", probe_base + 49 * _MINUTE),
            ]
            user_sessions.append({
                "user_id": user_id,
                "session_id": probe_session_id,
                "messages": probe_messages,
            })
            key = lambda idx: f"{probe_session_id}#{idx}"
            queries.append(Query(
                f"q{u_idx:04d}-update-noise",
                user_id,
                "governance_update_noise",
                level,
                f"{project}目前生效的预算口径是多少？",
                [key(2)],
                [key(0), key(6)],
            ))
            queries.append(Query(
                f"q{u_idx:04d}-direct-preference",
                user_id,
                "direct_preference",
                level,
                "我做样本抽检时更喜欢用什么工具？",
                [key(4)],
                [key(3), key(5)],
            ))

        for sess in user_sessions:
            sess.pop("_base", None)
            sessions.append(sess)

    return Dataset(seed=seed, scale=scale, difficulty=difficulty, suite=suite,
                   users=users, sessions=sessions, queries=queries)


__all__ = ["Dataset", "Query", "make_dataset", "SCALES", "DIFFICULTIES", "SUITES"]
