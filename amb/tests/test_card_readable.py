"""Test the human-readable card paths without opening any live Linear cards."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import amb
from tests import test_direct_card_rules
from tests.test_direct_card_rules import direct
from tests.test_linear_app import MockLinear, app
from tests.test_t5 import T5Base


@pytest.mark.parametrize("report", [
    '{"summary":"已通过","open_items":[],"goal_met":true}',
    '{\n  "summary":"已通过",\n  "open_items":[{"what":"剩余项"}],\n  "goal_met":false\n}',
    '```json\n{"summary":"已通过"}\n```',
])
def test_human_result_strips_machine_report_and_keeps_evidence(report):
    raw = ("验收通过。\n\n# <REDACTED_ID> done · 第 1 次 · 执行 sol\n"
           "结论：验收通过。\n证据：pytest 通过；复核通过；sha 123；已合并（合并前 456）\n"
           "证据：flake8 未通过；未复核；sha 无；未合并\n详情：<REDACTED_PATH>\n"
           "结论：还需要另一次核对。\n\n" + report)
    expected = ("验收通过。\n\n证据：pytest 通过；复核通过\n"
                "证据：flake8 未通过；未复核\n结论：还需要另一次核对。")
    assert app.human_result(raw) == expected
    assert app.human_result('保留示例\n{"summary":"示例"}\n这是正文。').endswith("这是正文。")


def test_progress_overflow_keeps_headings_and_file_links(monkeypatch):
    uploaded = {}

    def upload(path):
        uploaded[path.name] = path.read_text()
        return "https://example.com/" + str(len(uploaded))

    monkeypatch.setattr(app, "upload", upload)
    monkeypatch.setattr(app, "DESCRIPTION_BYTES", 1024)
    current, next_steps = "已经核对的内容。" * 2000, "还要核对的事项。" * 500
    blocks = {"progress": "## 现在到哪了\n" + current, "next": "## 下一步\n" + next_steps,
              "products": "## 产物\n文档"}
    app.fit_progress({"identifier": "<REDACTED_ID>"}, "", blocks, ("progress", "next"))
    assert uploaded == {"<REDACTED_ID>-现在到哪了.md": current, "<REDACTED_ID>-下一步.md": next_steps}
    assert "已经核对" in blocks["progress"]
    assert "还要核对" in blocks["next"]
    assert "全文：[<REDACTED_ID>-现在到哪了.md]" in blocks["progress"]
    assert "全文：[<REDACTED_ID>-下一步.md]" in blocks["next"]
    assert blocks["products"] == "## 产物\n文档"
    assert len(app.description("", blocks).encode()) <= 1024


def test_old_same_name_media_entries_are_removed():
    table = "| 卡 |\n| --- |\n| 旧数据 |"
    old = ("## 直接看\n说明\n\n"
           "gpu.csv：卡上是压缩预览，原片在 <REDACTED_HOST_B> 的 `/tmp/old.csv`\n\n"
           f"{table}\n\n[gpu.csv](<https://example.com/old1>)\n\n"
           f"{table}\n\n[gpu.csv](https://example.com/old2)\n\n"
           "![其他.png](https://example.com/other)")
    assert app.remove_media(old, "gpu.csv") == "## 直接看\n说明\n\n![其他.png](https://example.com/other)"


def test_attach_replaces_a_table_and_still_deduplicates_urls(tmp_path, monkeypatch):
    api = MockLinear()
    monkeypatch.setattr(app, "gql", api.gql)
    monkeypatch.setattr(app, "upload", lambda _: "https://example.com/new")
    _, blocks = app.segments(api.card)
    blocks["media"] = "## 直接看\n\n| 旧数据 |\n| --- |\n\n[gpu.csv](https://example.com/old)"
    api.card["description"] = app.description("", blocks)
    table = tmp_path / "gpu.csv"
    table.write_text("卡\n新数据\n")
    same_url = tmp_path / "duplicate.csv"
    same_url.write_bytes(table.read_bytes())
    assert app.attach_media(api.card, [table, same_url]) == ["gpu.csv"]
    assert "旧数据" not in api.card["description"]
    assert api.card["description"].count("gpu.csv") == 1
    assert "新数据" in api.card["description"]


def test_humanize_separates_full_width_url_punctuation():
    for punct in "。，；：）】》":
        assert app.humanize("看 https://example.com/a?x=1" + punct) == "看 https://example.com/a?x=1 " + punct
    link = "[查看](https://example.com/a)。"
    assert app.humanize(link) == link


@pytest.mark.parametrize("brief, words, key, content", [
    ("运维说明。不需重新部署。", None, "dispatch-note", "运维说明。"),
    ("内部说明。\n童成卓说“只改卡面”。", None, "words", "“只改卡面”"),
    ("背景“这段不算”。", "明确给到的用户原话", "words", "明确给到的用户原话"),
])
def test_new_card_labels_real_user_words(brief, words, key, content, monkeypatch):
    api = MockLinear()
    monkeypatch.setattr(app, "gql", api.gql)
    flags = ["--user-words", words] if words else []
    app.main(["new", "改卡面", brief, "--goal", "卡面能直接读出结果。完成标准：测试通过。", *flags])
    _, blocks = app.segments(api.card)
    assert content in blocks[key]
    assert ({"dispatch-note", "words"} & blocks.keys()) == {key}
    if key == "words":
        assert "内部说明" not in blocks[key] and "不算" not in blocks[key]
    _, legacy = app.legacy_segments("+++ 派单时的说明\n\n> 例行检查\n\n+++", "检查")
    assert "例行检查" in legacy["dispatch-note"]


class TestAmbCancellation(T5Base):
    def test_one_off_cancellation_writes_canceled_with_one_reason(self):
        with patch.object(amb, "jobs_dir", side_effect=lambda *p: str(self.jobs.joinpath(*p))), \
                patch.object(amb, "linear_app", return_value="ok") as linear:
            amb.linear_sync_state(
                {"id": "J-test", "linear": "<REDACTED_ID>", "linear_auto": True},
                {"state": "cancelled", "history": [{"to": "cancelled", "why": "用户调整方向\n第二行"}]}, {})
        assert linear.call_args.args[1:4] == ("state", "<REDACTED_ID>", "Canceled")
        assert Path(linear.call_args.args[4]).read_text() == "用户调整方向\n"


def test_direct_cancellation_and_result_description(tmp_path, monkeypatch):
    api = MockLinear()
    monkeypatch.setattr(app, "gql", api.gql)
    with patch.object(direct, "linear_app", return_value="ok") as linear:
        direct.sync_card(tmp_path, {"linear": "<REDACTED_ID>", "phase": "cancelled", "error": "用户调整方向"}, "")
    command, card, state, path = linear.call_args.args
    assert (command, card, state) == ("state", "<REDACTED_ID>", "Canceled")
    assert Path(path).read_text() == "用户调整方向\n"
    app.set_state(SimpleNamespace(state=state, file=path, owner_msg=None), api.card, {})
    assert api.card["description"].splitlines()[0] == "取消原因：用户调整方向"
    assert "## 结果" not in api.card["description"]


def test_pending_text_has_no_hash_or_double_period():
    activity = {"pending": {
        "123abc": {"item": {"what": "检查报表。", "eta": None, "owner": "Sol"}},
        "456def": {"item": "等验收"},
    }}
    text = amb.append_pending("## 现在到哪了\n已完成。\n\n## 下一步\n- 做验收。", activity)
    public, reminder = text.split("\n\n给 AI 的提醒：")
    assert "123abc" not in public and "456def" not in public and "。。" not in public
    assert "- 未收尾：检查报表。完成时间待定，由 Sol 收结果。" in public
    assert reminder.strip() == "未收尾事项编号 123abc、456def（amb progress --resolve 用）"
    described = app.description("", {"ai": app.HEADING["ai"] + reminder.strip(), "words": "字句"})
    assert described.endswith(reminder.strip())
    assert set(app.segments({"description": described, "title": "验收"})[1]) == {"words", "ai"}


def test_progress_cleans_body_and_keeps_then_clears_pending_reminder(tmp_path, monkeypatch):
    api = MockLinear()
    monkeypatch.setattr(app, "gql", api.gql)
    path = tmp_path / "progress.md"
    text = ("## 现在到哪了\n# <REDACTED_ID> done · 第 1 次 · 执行 sol\n"
            "已检查 https://example.com/a。\n详情：<REDACTED_PATH>\n\n"
            "## 下一步\n- 接着核对。")
    path.write_text(text + '\n\n给 AI 的提醒：未收尾事项编号 abcd（amb progress --resolve 用）\n\n'
                    '{"summary":"不该显示"}')
    app.apply_progress(api.card["identifier"], path, card=api.card)
    desc = api.card["description"]
    assert "https://example.com/a 。" in desc and "详情：" not in desc and '"summary"' not in desc
    assert desc.endswith("给 AI 的提醒：未收尾事项编号 abcd（amb progress --resolve 用）")
    path.write_text(text)
    app.apply_progress(api.card["identifier"], path, card=api.card)
    assert "abcd" not in api.card["description"]


def test_open_item_job_owner_uses_task_executor_name(tmp_path, monkeypatch):
    monkeypatch.setenv("AMB_ROOT", str(tmp_path))
    amb.write_json(amb.jobs_dir("<REDACTED_ID>", "job.json"), {"title": "核对交付"})
    text = amb.open_item_text({"what": "检查报表。", "eta": None, "owner": "<REDACTED_ID>"})
    assert text == "检查报表。完成时间待定，由 「核对交付」的执行者 收结果。"


def test_clip_card_text_favors_a_complete_sentence():
    assert amb.clip_card_text("甲完成。乙验收！丙还在运行。", 10) == "甲完成。乙验收！"
    assert amb.clip_card_text("甲乙丙丁戊", 3) == "甲乙…"
    assert amb.clip_card_text("好。", 2) == "好。"


@pytest.mark.parametrize("feishu", ["off", "failed", "sent"])
def test_dispatch_comment_keeps_prompt_in_task_record(feishu):
    dispatch = {"step": "核对报表", "summary": "先核对字段。"}
    entry = {"dispatch": dispatch, "body": "先核对字段。\n\n## 执行提示词\n这部分只留在任务记录。"}
    body = amb.dispatch_card_comment(entry, "fake-marker", feishu)
    assert "核对报表" in body
    assert "执行提示词" not in body and "这部分只留" not in body
    assert body.endswith("这一步的完整提示词已发到飞书" if feishu == "sent"
                         else "完整提示词这次没发到飞书，留在任务记录里")


def test_goal_must_explain_the_title():
    with pytest.raises(ValueError, match="不能只是重复标题"):
        amb.validate_card_fields("核对报表", "  核对报表。  ")
    amb.validate_card_fields("核对报表", "对照来源核对列值。完成标准：差异都有记录。")


@pytest.mark.parametrize("title", [None, "stop-dog2", "  "])
def test_direct_run_requires_a_readable_title(title, tmp_path, monkeypatch):
    monkeypatch.delenv("AMB_DIRECT_ROOT", raising=False)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("检查卡面。")
    args = test_direct_card_rules.DirectGoalTests().args(prompt, goal="核对进度和产物段。完成标准：可读。")
    args.title = title
    with patch.object(direct, "linear_app") as linear, pytest.raises(ValueError, match="必须给 --title"):
        direct.run_task(args, tmp_path / "runs")
    linear.assert_not_called()
    assert not (tmp_path / "runs").exists()


def test_direct_finish_saves_prose_and_machine_report_for_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("AMB_ROOT", str(tmp_path / "amb"))
    status = {"name": "test", "title": "检查卡面", "speed_hint": False, "linear": "<REDACTED_ID>"}
    result = {"summary": "检查通过。", "open_items": [], "goal_met": True}
    message = "这里是最后一段说明。\n\n" + json.dumps(result, ensure_ascii=False, indent=2)
    (tmp_path / "out.md").write_text("原有交付。")
    direct.finish(tmp_path, status, 0, last_message=message)
    assert (tmp_path / "out.md").read_text() == "原有交付。"
    assert (tmp_path / "summary.md").read_text() == "这里是最后一段说明。"
    assert json.loads((tmp_path / "final.json").read_text()) == result
    with patch.object(direct, "linear_app", return_value="ok") as linear:
        direct.sync_card(tmp_path, status, "")
    assert linear.call_args.args[:3] == ("state", "<REDACTED_ID>", "Done")
    assert Path(linear.call_args.args[3]).read_text() == "检查通过。\n\n原有交付。"


def test_text_attachments_are_supported(tmp_path):
    files = [tmp_path / "正文.md", tmp_path / "正文.txt"]
    for path in files:
        path.write_text("全文。")
    assert app.media_files(files) == files
    assert app.media_files(files, include_text=False) == []


def test_original_words_only_extracts_attributed_or_standalone_quotes_before_first_section():
    brief = (
        '# 卡片的“原话”段\n\n'
        '背景里提到“结果”和“直接看”。\n'
        '（02:20）“带时间的原话”\n'
        '童成卓说“按规则改”。\n'
        '用户说“保留原文”和“带时间”。\n'
        '“单独一行也是原话”\n\n'
        '## 要改的 3 条\n'
        '把“直接看”和“结果”整理一下。\n'
        '用户说“这是举例”。\n'
        '“这是示例，不是原话”\n'
    )
    assert app.original_words(brief, None) == (
        '“带时间的原话”\n“按规则改”\n“保留原文”\n“带时间”\n“单独一行也是原话”'
    )


def test_new_card_preserves_the_whole_marked_words_section(monkeypatch):
    api = MockLinear()
    monkeypatch.setattr(app, "gql", api.gql)
    brief = '# 核对报表\n\n## 用户原话\n请先看“结果”，然后逐项检查。\n材料：内部说明'
    app.main(["new", "核对报表", brief, "--goal", "确认报表里的字段都准确。"])
    _, blocks = app.segments(api.card)
    assert "> 请先看“结果”，然后逐项检查。" in blocks["words"]
    assert "内部说明" not in blocks["words"]
    assert "dispatch-note" not in blocks


@pytest.mark.parametrize("paragraph, expected", [
    ("确认每个字段。", "确认每个字段。"),
    ("先核对列名。" + "逐项检查" * 100 + "。", "先核对列名。"),
    ("字" * 301, "字" * 299 + "…"),
])
def test_new_card_without_words_uses_only_the_first_paragraph(paragraph, expected, monkeypatch):
    api = MockLinear()
    monkeypatch.setattr(app, "gql", api.gql)
    brief = f"# 报表校对\n\n## 背景\n\n{paragraph}\n\n第二段不在卡面显示。\n\n## 执行要求\n核对时间。"
    assert app.original_words(brief, None) == ""
    app.main(["new", "核对报表", brief, "--goal", "确认报表里的字段都准确。"])
    _, blocks = app.segments(api.card)
    assert blocks["dispatch-note"] == f"## 派单时的说明\n> {expected}"
    assert "words" not in blocks


def test_direct_run_forwards_user_words_file_and_keeps_asked_at(tmp_path, monkeypatch):
    monkeypatch.delenv("AMB_DIRECT_ROOT", raising=False)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("检查卡面。")
    words = tmp_path / "用户原话.md"
    words.write_text("保留我的原话。")
    asked = "2026-11-22T18:20:00+00:00"
    monkeypatch.setattr(direct.sys, "argv", [
        "amb-direct-run", "run", "words-test", "--once", "--prompt", str(prompt),
        "--title", "检查卡面", "--goal", "核对卡面原话是否完整。",
        "--asked-at", asked, "--user-words-file", str(words),
    ])
    monkeypatch.setattr(direct.signal, "signal", lambda *_: None)
    with patch.object(direct, "linear_app", side_effect=RuntimeError("stop at new")) as linear, \
            patch.object(direct, "lead_card", return_value=None), \
            patch.object(direct.Path, "resolve", return_value=tmp_path / "runs"):
        with pytest.raises(RuntimeError, match="stop at new"):
            direct.main()
    call = linear.call_args.args
    assert call[0] == "new"
    assert call[call.index("--user-words-file") + 1] == str(words)
    assert call[call.index("--received-at") + 1] == asked
