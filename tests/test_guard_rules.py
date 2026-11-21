"""The stale-write rules (commons.core.guard_rules), without Hermes."""

from __future__ import annotations

from commons.core import guard_rules as gr

K = ("s:1", "/shared/x/SKILL.md")


def test_full_write_needs_the_version_this_session_read():
    m = gr.Memory()
    assert not gr.decide_full_write(m, K, "v1").allow  # never read
    m.saw(K, "v1")
    assert gr.decide_full_write(m, K, "v1").allow
    assert not gr.decide_full_write(m, K, "v2").allow  # someone else wrote v2


def test_creating_a_missing_file_needs_no_read():
    assert gr.decide_full_write(gr.Memory(), K, gr.ABSENT).allow


def test_a_file_removed_since_the_read_is_refused():
    m = gr.Memory()
    m.saw(K, "v1")
    v = gr.decide_full_write(m, K, gr.ABSENT)
    assert not v.allow and "removed" in v.reason


def test_a_read_in_the_same_response_does_not_count():
    m = gr.Memory()
    m.saw(K, "v1", api_request_id="req-1")
    assert not gr.decide_full_write(m, K, "v1", api_request_id="req-1").allow
    assert gr.decide_full_write(m, K, "v1", api_request_id="req-2").allow


def test_own_confirmed_write_becomes_the_base():
    m = gr.Memory()
    m.saw(K, "v1")
    m.will_write(K, "v2")
    assert gr.decide_full_write(m, K, "v2").allow  # pending is expected
    m.wrote(K, ok=True)
    assert gr.decide_full_write(m, K, "v2").allow
    assert not gr.decide_full_write(m, K, "v1").allow  # only one version: no going back


def test_a_failed_write_keeps_the_old_base():
    m = gr.Memory()
    m.saw(K, "v1")
    m.will_write(K, "v2")
    m.wrote(K, ok=False)
    assert gr.decide_full_write(m, K, "v1").allow


def test_an_unpredictable_write_forces_a_re_read():
    m = gr.Memory()
    m.saw(K, "v1")
    m.will_write(K, gr.UNKNOWN)
    assert not gr.decide_full_write(m, K, "v1").allow
    m.wrote(K, ok=True)
    assert not gr.decide_full_write(m, K, "whatever").allow
    m.saw(K, "v3")
    assert gr.decide_full_write(m, K, "v3").allow


def test_exact_patch_passes_without_any_read():
    v = gr.decide_patch(gr.Memory(), K, "v9", "alpha\nbeta\n", "beta")
    assert v.allow


def test_inexact_patch_needs_an_unchanged_file():
    m = gr.Memory()
    m.saw(K, "v1")
    assert gr.decide_patch(m, K, "v1", "alpha\n", "alpah").allow  # fuzzy, but nothing changed
    assert not gr.decide_patch(m, K, "v2", "alpha\n", "alpah").allow


def test_predicted_patch_matches_hermes_single_replacement():
    assert gr.predicted_patch("a b a", "b", "c", False) == gr.digest_text("a c a")
    assert gr.predicted_patch("a a", "a", "c", False) is None  # ambiguous: Hermes refuses it
    assert gr.predicted_patch("a a", "a", "c", True) == gr.digest_text("c c")
    assert gr.predicted_patch("abc", "zz", "c", False) is None


def test_memory_is_capped():
    m = gr.Memory(max_entries=3)
    for i in range(5):
        m.saw(("s", str(i)), "v")
    assert not m.expected(("s", "0"))[0] and m.expected(("s", "4"))[0]


def test_read_version_of_a_missing_and_an_oversize_file(tmp_path):
    assert gr.read_version(tmp_path / "nope") == (gr.ABSENT, None)
    big = tmp_path / "big"
    big.write_bytes(b"x" * (gr.MAX_FILE_BYTES + 1))
    assert gr.read_version(big) is None


def test_block_message_tells_the_agent_how_to_recover(tmp_path):
    msg = gr.block_message(tmp_path / "SKILL.md", "it changed")
    assert "nothing was written" in msg and "read it again" in msg and "Do not resend" in msg
