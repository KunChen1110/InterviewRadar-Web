"""内置示例题（CS/计算机就业方向）。

⚠️ 占位数据：用于骨架阶段跑通闭环，来源字段为示例标注。
第 2 步接入真实面经知识库（questions 表 + pgvector 检索）后，
build_plan 将改为"RAG 召回 + LLM 锚定"，本文件仅作兜底。
"""
from __future__ import annotations

from profile import project_names

CS_TRACK = "cs"

SEED_QUESTIONS: list[dict] = [
    {
        "step": "自我介绍",
        "topic": "候选人定位",
        "anchor": "整体简历",
        "desc": "考察表达结构、岗位动机和项目主线是否清晰。",
        "question": "请用 60-90 秒做一个自我介绍，重点说明你为什么适合这个岗位。",
        "followups": ["你刚才提到项目经历，请挑一个最能证明岗位匹配度的项目展开讲。"],
        "tags": ["基础"],
        "sources": [{"name": "示例来源 · 校招面经", "url": "", "note": "占位，待真实语料替换"}],
    },
    {
        "step": "项目深挖",
        "topic": "项目技术选型",
        "anchor": "简历中的核心项目",
        "desc": "考察你是否真正理解自己项目的技术决策，而不是只会背概念。",
        "question": "挑一个你简历里最有代表性的项目，讲讲它的整体架构，以及你在其中负责的核心模块。",
        "followups": [
            "这个技术选型当时有哪些备选方案？你为什么没选另一个？",
            "如果让你重做这个项目，你会改掉哪个设计？为什么？",
        ],
        "tags": ["项目"],
        "sources": [{"name": "示例来源 · 实习面经", "url": "", "note": "占位，待真实语料替换"}],
    },
    {
        "step": "项目深挖",
        "topic": "难点与量化结果",
        "anchor": "项目中的难点描述",
        "desc": "考察你解决问题的方法和结果是否可量化，识别'写得虚'的部分。",
        "question": "这个项目里你遇到的最大技术难点是什么？具体怎么定位、怎么解决的？",
        "followups": [
            "你刚才说'性能提升明显'，具体指标是什么？怎么测的？",
            "如果没有现成工具或标注数据，你怎么验证你的方案有效？",
        ],
        "tags": ["项目", "量化"],
        "sources": [{"name": "示例来源 · 校招面经", "url": "", "note": "占位，待真实语料替换"}],
    },
    {
        "step": "能力匹配",
        "topic": "数据结构与算法",
        "anchor": "岗位基础要求",
        "desc": "考察 CS 基础扎实程度，是大多数技术岗的必问项。",
        "question": "讲讲哈希表的实现原理。发生哈希冲突时有哪些处理方式，各自的代价是什么？",
        "followups": ["什么情况下哈希表会退化？工程上怎么规避（比如 Redis 的渐进式 rehash）？"],
        "tags": ["八股", "数据结构"],
        "sources": [{"name": "示例来源 · 高频面经", "url": "", "note": "占位，待真实语料替换"}],
    },
    {
        "step": "能力匹配",
        "topic": "操作系统与网络",
        "anchor": "岗位基础要求",
        "desc": "考察对进程/线程、网络模型的理解，后端方向高频。",
        "question": "进程和线程的区别是什么？TCP 三次握手为什么不是两次？",
        "followups": ["如果线上服务出现大量 TIME_WAIT，你会怎么排查和处理？"],
        "tags": ["八股", "OS", "网络"],
        "sources": [{"name": "示例来源 · 高频面经", "url": "", "note": "占位，待真实语料替换"}],
    },
    {
        "step": "能力匹配",
        "topic": "数据库",
        "anchor": "岗位基础要求",
        "desc": "考察索引、事务、锁的理解深度。",
        "question": "MySQL 的索引底层为什么用 B+ 树而不是哈希或二叉树？什么是覆盖索引？",
        "followups": ["一条慢 SQL 摆在你面前，你的排查顺序是什么？"],
        "tags": ["八股", "数据库"],
        "sources": [{"name": "示例来源 · 高频面经", "url": "", "note": "占位，待真实语料替换"}],
    },
    {
        "step": "场景问题",
        "topic": "系统设计",
        "anchor": "岗位方向 + 项目经验",
        "desc": "考察把基础知识点组合成工程方案的能力。",
        "question": "如果让你设计一个秒杀系统，你会怎么处理高并发下的库存扣减？",
        "followups": ["缓存和数据库的一致性怎么保证？超卖怎么防？"],
        "tags": ["系统设计", "高并发"],
        "sources": [{"name": "示例来源 · 高频面经", "url": "", "note": "占位，待真实语料替换"}],
    },
    {
        "step": "场景问题",
        "topic": "岗位匹配总结",
        "anchor": "整体表现",
        "desc": "考察你能否主动总结自己和岗位的匹配度。",
        "question": "最后，用三点总结一下：为什么我们应该录用你，而不是其他候选人？",
        "followups": [],
        "tags": ["收尾"],
        "sources": [{"name": "示例来源 · 校招终面", "url": "", "note": "占位，待真实语料替换"}],
    },
]

# 各强度对应的题量（从 SEED 中截取，项目深挖题始终保留）
_MODE_LEN = {"light": 5, "normal": 7, "hard": 8}


def build_plan(track: str, role: str, mode: str, profile: dict) -> list[dict]:
    """骨架版计划：按强度截取内置题；第 3 步改为 RAG 召回 + LLM 锚定。"""
    n = _MODE_LEN.get(mode, 7)
    plan = [dict(q) for q in SEED_QUESTIONS[:n]]
    # 若简历解析出了项目，把它写进项目题的锚点，让问题更贴人
    if project_names(profile):
        first_project = project_names(profile)[0][:40]
        for q in plan:
            if q["step"] == "项目深挖":
                q["anchor"] = first_project
                break
    return plan
