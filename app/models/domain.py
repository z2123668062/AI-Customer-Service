# app/models/domain.py
"""数据库模型：业务三张表（卡片 cards / 线 edges / 原文 messages）。

对齐《系统设计.md》§2 数据模型；一致性铁律见《工程规范.md》§2。

- messages: 归档明文，永久保留——卡片是有损抽象，原文才是真相（二级回源在它里面搜）
- cards:    记忆的基本单元。kind=entity（会被事件改写的最新状态）/ event（一卡一动作）
            / task（任务区占位，V1 不实现）。status 是"物化视图 + 权威标记"混合：
            active/superseded 由 edges 推导（取代关系以 edges 为真相，"插边 + 改状态"同事务）；
            revoked/evicted 是权威值、不由边推导（rebuild 保持不动）——原语见 store.py
- edges:    线（关系）。caused_by：事件→事件（只存"原因"单方向，结果靠反查）/
            led_to：事件→实体卡 / supersedes：新→旧（版本化，回答"什么时候搬的家"）；
            cut_at 非空 = 已剪断（E11 编辑层：标记式保留审计，不参与顺线/回填/版本重建）

取代旧设计：原 memory 表（fact/episode 二分 + superseded_by 指针）已废弃删除。
写路径进度（水位）不在库里——由追加式 journal 日志承载（D23，见《写路径文档.md》）。
"""
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.core.database import Base

# —— 取值约定（应用层枚举；全库引用这些常量，勿散写字符串）——
CARD_KINDS = ("entity", "event", "task")  # task 占位，V1 不实现
# 前四类是"用户自己的属性"，后四类是"可被事件指向的节点"（L7，2026-10-31）。
# 后四类存在的唯一理由：让 led_to 有端点可连。原先人/地/物只是卡上的字符串，
# 没有 id 就建不了边，全库 209 张卡只有 8 条边就是这么来的。
CARD_SUBTYPES_ATTR = ("identity", "preference", "goal", "relation")
CARD_SUBTYPES_NODE = ("person", "place", "org", "thing")
CARD_SUBTYPES_ENTITY = CARD_SUBTYPES_ATTR + CARD_SUBTYPES_NODE
CARD_STATUSES = ("active", "superseded", "revoked", "evicted")  # evicted = 衰减下架（E11）
EDGE_KINDS = ("caused_by", "led_to", "supersedes")
CARD_SOURCES = ("passive", "explicit")


class Card(Base):
    """卡片：点是记忆（系统设计 §2.2）。实体/事件同表不同 kind——线要连任意卡，统一 ID 空间最省事。"""

    __tablename__ = "cards"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String(20), index=True, nullable=False,
                  comment="entity（状态）/ event（发生）/ task（占位，V1 不实现）")
    subtype = Column(String(50), nullable=True,
                     comment="entity：身份/偏好/目标/关系；event：动作类型〔待议〕")
    content = Column(Text, nullable=False, comment="卡片正文（有损抽象；原文见 messages）")
    entities = Column(JSON, nullable=True, comment="涉及的人地物（实体入口倒排来源），JSON 字符串数组")
    canonical_name = Column(String(120), nullable=True, index=True,
                            comment="实体卡的标准名（person/place/org/thing 用）：别名归并的主键，"
                                    "Melanie 与 Mel 指向同一张卡；属性卡与事件卡留空")
    source_message_ids = Column(JSON, nullable=True,
                                comment="来源原文 id 数组（L7）：卡片是有损抽象，这张表记它从哪几条"
                                        "messages 抽出来，注入时可无损回带原文，不必靠关键词猜")
    event_time = Column(DateTime(timezone=True), nullable=True, comment="事件时间（目标按此过期；实体卡留空）")
    status = Column(String(20), default="active", index=True, nullable=False,
                    comment="active（有效）/ superseded（已被取代）/ revoked（已撤销）"
                            "/ evicted（衰减下架，E11）；superseded/active 由 edges 推导，"
                            "revoked/evicted 为权威值（rebuild 不动）")
    importance = Column(Float, nullable=True, comment="重要性（写时不判断，读时/淘汰时才算）")
    source = Column(String(20), default="passive", nullable=False,
                    comment="passive（对话自动抽取）/ explicit（用户显式『记住』）")
    source_session_id = Column(String(255), index=True, nullable=True,
                               comment="溯源会话（弱引用：会话本身不落库）")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_accessed_at = Column(DateTime(timezone=True), nullable=True, comment="最后访问时间（淘汰/续命依据）")
    access_count = Column(Integer, default=0, comment="访问计数（淘汰/续命依据）")


class Edge(Base):
    """线：关系（系统设计 §2.3）。因果允许缺失（空槽=还没有这条线，不占存储）；每条线记来源，可见、可剪断。"""

    __tablename__ = "edges"

    id = Column(Integer, primary_key=True, index=True)
    from_card_id = Column(Integer, ForeignKey("cards.id"), index=True, nullable=False,
                          comment="起点卡（supersedes 时为『新』）")
    to_card_id = Column(Integer, ForeignKey("cards.id"), index=True, nullable=False,
                        comment="终点卡（supersedes 时为『旧』；caused_by 反查结果也走此列）")
    kind = Column(String(20), index=True, nullable=False,
                  comment="caused_by（事件→事件）/ led_to（事件→实体卡）/ supersedes（新→旧）")
    source_session_id = Column(String(255), nullable=True, comment="溯源会话（这条线在哪里被连上的）")
    cut_at = Column(DateTime(timezone=True), nullable=True,
                    comment="剪断时间（E11 编辑层）：NULL=未剪断；非空=已剪断（标记式保留审计，"
                            "不参与顺线/回填/版本重建）")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    """原文底档：归档明文，永久保留（系统设计 §2.1 硬规定②；结构自旧版沿用，与设计一致）。"""

    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(255), index=True, comment="会话标识（溯源：这条记录来自哪次对话）")
    role = Column(String(50), comment="user / assistant")
    content = Column(Text, comment="消息明文（永久保留）")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
