"""journal 单测（D23）：行格式成对 / 尾读容错 / 追加双落点 / 水位缓存与推进。

纯同步模块（无 DB / 无网络）；路径经 `journal._journal_file` 注入临时文件。
"""
import pytest

from app.services.long_term_memory import journal
from app.services.long_term_memory.extractor import CardDraft


@pytest.fixture
def jpath(tmp_path, monkeypatch):
    """独立 journal 临时文件 + 清内存水位（monkeypatch 自动还原路径注入）。"""
    p = tmp_path / "extract_journal.log"
    monkeypatch.setattr(journal, "_journal_file", str(p))
    journal.reset_for_tests()
    yield p
    journal.reset_for_tests()


# ================================
# ① 格式与解析（成对锁定）
# ================================

def test_success_line_roundtrip_with_stats():
    drafts = [CardDraft(kind="entity", subtype="preference", content="用户喜欢喝咖啡"),
              CardDraft(kind="entity", subtype="identity", content="用户住在上海"),
              CardDraft(kind="event", content="用户上周去了杭州")]
    line = journal.format_success(watermark=123, messages=8, drafts=drafts, duplicates=0,
                                  superseded=1, edges=2, sessions={"s1"}, elapsed=3.2)
    assert "抽水完成" in line and "watermark=123" in line
    assert "原文=8条" in line and "卡片=3张" in line
    assert "entity:2（preference:1, identity:1）" in line and "event:1" in line
    assert "重复=0" in line and "取代=1" in line and "连边=2" in line
    assert "耗时=3.2s" in line and "会话=s1" in line

    rec = journal.parse_record(line)
    assert rec is not None
    assert rec["watermark"] == 123 and rec["event"] == "抽水完成"
    assert rec["ts"]                                        # 时间戳提取


def test_failure_line_roundtrip():
    line = journal.format_failure(watermark=42, error="TimeoutError: timed out\n第二行折行")
    assert "抽水失败" in line and "watermark=42" in line
    assert "TimeoutError" in line and "\n" not in line       # 折行被折叠为一行
    rec = journal.parse_record(line)
    assert rec["watermark"] == 42 and rec["event"] == "抽水失败"


def test_generic_record_roundtrip_for_line_and_heal_events():
    line = journal.format_record(event="首启画线", watermark=7, detail="跳过存量 7 条")
    rec = journal.parse_record(line)
    assert rec["watermark"] == 7 and rec["event"] == "首启画线"


def test_parse_record_rejects_broken_lines():
    assert journal.parse_record("") is None
    assert journal.parse_record("这不是一行有效记录") is None
    assert journal.parse_record("2026-09-29 | 抽水完成") is None   # 无 watermark=


# ================================
# ② 尾读容错（回退 + 缺失）
# ================================

def test_read_last_record_returns_none_when_file_missing(jpath):
    assert journal.read_last_record() is None
    assert journal.read_watermark() is None


def test_read_watermark_takes_last_line(jpath):
    journal.append_record(journal.format_record(event="抽水完成", watermark=5))
    journal.append_record(journal.format_record(event="抽水完成", watermark=10))
    journal.append_record(journal.format_record(event="抽水失败", watermark=10))
    assert journal.read_watermark() == 10
    assert journal.read_last_record()["event"] == "抽水失败"


def test_read_watermark_falls_back_over_broken_tail(jpath):
    """尾行损坏 → 回退上一条有效行（重抽最后一批，方向安全）。"""
    journal.append_record(journal.format_record(event="抽水完成", watermark=15))
    with open(jpath, "a", encoding="utf-8") as f:
        f.write("半行损坏记录……\n")
        f.write("\n")
    assert journal.read_watermark() == 15


def test_read_watermark_none_when_all_broken(jpath):
    with open(jpath, "w", encoding="utf-8") as f:
        f.write("垃圾行一\n垃圾行二\n")
    assert journal.read_watermark() is None


# ================================
# ③ 追加（双落点 + 失败方向）
# ================================

def test_append_creates_parent_dir_and_adds_lines(tmp_path, monkeypatch):
    nested = tmp_path / "deep" / "dir" / "journal.log"
    monkeypatch.setattr(journal, "_journal_file", str(nested))
    journal.reset_for_tests()
    assert journal.append_record(journal.format_record(event="抽水完成", watermark=1))
    assert journal.append_record(journal.format_record(event="抽水完成", watermark=2))
    lines = nested.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2 and "watermark=2" in lines[-1]


def test_append_failure_returns_false(tmp_path, monkeypatch):
    """路径不可写（指向目录）→ 返回 False（调用方据此不推水位）。"""
    monkeypatch.setattr(journal, "_journal_file", str(tmp_path))
    journal.reset_for_tests()
    assert journal.append_record(journal.format_record(event="抽水完成", watermark=1)) is False


# ================================
# ④ 水位缓存与推进（advance 失败不推进）
# ================================

def test_current_reads_file_when_unloaded_and_caches(jpath):
    journal.append_record(journal.format_record(event="抽水完成", watermark=7))
    journal.reset_for_tests()                                # 清缓存 → 重新加载
    assert journal.current() == 7
    journal.append_record(journal.format_record(event="抽水完成", watermark=9))
    assert journal.current() == 7                            # 缓存优先（不再重复读文件）


def test_current_zero_when_no_record(jpath):
    assert journal.current() == 0                            # 无文件 = 尚未抽过


def test_advance_updates_cache_only_on_success(jpath):
    line = journal.format_record(event="抽水完成", watermark=3)
    assert journal.advance(3, line)
    assert journal.current() == 3


def test_advance_failure_keeps_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "_journal_file", str(tmp_path))    # 不可写路径
    journal.reset_for_tests()
    assert journal.advance(5, "x | 抽水完成 | watermark=5") is False
    assert journal.current() == 0                            # 失败不推进


def test_prime_sets_cache_without_writing_file(jpath):
    journal.prime(11)
    assert journal.current() == 11
    assert journal.read_watermark() is None                  # 文件未被写（prime 只改内存）
