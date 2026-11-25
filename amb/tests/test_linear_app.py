import contextlib
import copy
from datetime import timedelta
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


STAGED = Path(__file__).resolve().parents[1] / "linear-app"
PATH = STAGED if STAGED.is_file() else Path.home() / "bin" / "linear-app"
loader = SourceFileLoader("linear_app_under_test", str(PATH))
app = module_from_spec(spec_from_loader(loader.name, loader))
loader.exec_module(app)


class MockLinear:
    def __init__(self, long=False):
        self.card = {"id": "issue-id", "identifier": "<REDACTED_ID>", "title": "目标",
                     "description": app.description("", {
                         "goal": "**目标**：保留目标",
                         "progress": "## 现在到哪了\n旧现状",
                         "next": "## 下一步\n旧下一步",
                         "products": "## 产物\n[产物](https://example.com/)",
                         "words": "## 你的原话（全文）\n> 字字保留 J-1122-ABC",
                     }),
                     "state": {"name": "In Progress", "id": app.STATES["In Progress"]},
                     "project": {"id": "research"}, "updatedAt": "2026-11-22T06:00:00Z",
                     "labels": {"nodes": [{"id": app.LONG_LABEL}] if long else []}}
        self.comments = []
        self.history = []
        self.children = []
        self.creates = []
        self.updates = []
        self.tamper = False

    def gql(self, query, variables=None):
        v = variables or {}
        if "issueCreate" in query:
            self.creates.append(v["in"])
            self.card["description"] = v["in"]["description"]
            return {"issueCreate": {"success": True, "issue": {
                **self.card, "url": "https://example.com/issue"}}}
        if "issueUpdate" in query:
            inp = v["in"]
            self.updates.append(inp)
            if "description" in inp:
                self.card["description"] = inp["description"][:-1] if self.tamper else inp["description"]
            if "stateId" in inp:
                self.card["state"] = {"id": inp["stateId"], "name": next(
                    name for name, identifier in app.STATES.items() if identifier == inp["stateId"])}
            return {"issueUpdate": {"success": True, "issue": self.card}}
        if "commentCreate" in query:
            row = {"id": str(len(self.comments)), "body": v["b"]}
            self.comments.append(row)
            return {"commentCreate": {"success": True, "comment": row}}
        if "comment(id:" in query:
            return {"comment": self.comments[int(v["i"])]}
        for field in ("history", "comments", "children"):
            if f"{field}(first:" not in query:
                continue
            return {"issue": {field: {"nodes": getattr(self, field),
                                      "pageInfo": {"hasNextPage": False}}}}
        if "issue(id:" in query:
            return {"issue": copy.deepcopy(self.card)}
        if "projects(first:" in query:
            return {"projects": {"nodes": [{"id": "research"}]}}
        raise AssertionError(query)


class CardRules(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.enterContext(patch.object(app, "D", self.directory.name))
        self.enterContext(patch.object(app, "report_error"))

    def test_edit_card_updates_fields_without_reopening_logic(self):
        api = self.setup_api()
        intent = SimpleNamespace(title="更新标题", description_file=None, top=False,
                                 parent=None, branch=None, milestone=None)
        with contextlib.redirect_stdout(StringIO()):
            app.edit_card(intent, api.card)
        self.assertEqual(api.updates, [{"title": "更新标题"}])

    def setup_api(self, long=False):
        api = MockLinear(long)
        self.enterContext(patch.object(app, "gql", api.gql))
        return api

    def path(self, text):
        path = Path(self.directory.name) / "input.md"
        path.write_bytes(text.encode())
        return path

    def cli(self, *args):
        with contextlib.redirect_stdout(None):
            app.main([str(a) for a in args])

    def test_alignment_comment_retry_posts_full_text_once(self):
        remote = MockLinear()
        body = "派单提示词\n原样保留 Unicode、空格和尾部换行。  \n"
        with patch.object(app, "gql", side_effect=remote.gql):
            self.cli("comment", "<REDACTED_ID>", body, "--once")
            self.cli("comment", "<REDACTED_ID>", body, "--once")
        self.assertEqual([row["body"] for row in remote.comments], [body])
        self.assertEqual(remote.updates, [])

    def setup_comment_api(self, author, bot=None, tamper=False):
        card = {"id": "issue-id", "identifier": "<REDACTED_ID>"}
        remote = {"id": "comment-id", "body": "原文", "user": author, "botActor": bot}
        mutations = []

        def gql(query, variables):
            if query.startswith("query"):
                self.assertEqual(variables, {"i": card["id"], "c": "comment-id"})
                return {"viewer": {"id": "ambition-id"},
                        "issue": {"comments": {"nodes": [remote.copy()] if remote else []}}}
            mutations.append(query)
            if "commentUpdate" in query:
                body = variables["input"]["body"]
                remote["body"] = body.rstrip() if tamper else body
                return {"commentUpdate": {"success": True}}
            if "commentDelete" in query:
                path = Path(self.directory.name) / "deleted-comments.jsonl"
                receipt = json.loads(path.read_text())
                self.assertEqual((receipt["issue"], receipt["comment_id"], receipt["body"]),
                                 (card["identifier"], "comment-id", "原文"))
                self.assertTrue(receipt["ts"])
                remote.clear()
                return {"commentDelete": {"success": True}}
            self.fail(query)

        self.enterContext(patch.object(app, "writable_issue", return_value=card))
        self.enterContext(patch.object(app, "gql", side_effect=gql))
        self.enterContext(patch.object(app, "READBACK_DELAYS", (0,)))
        return remote, mutations

    def test_edit_and_delete_reject_other_authors(self):
        remote, mutations = self.setup_comment_api({"id": "someone-else"})
        for command, args in (("comment-edit", (self.path("新正文"),)), ("comment-delete", ())):
            with self.subTest(command=command), self.assertRaisesRegex(app.WriteRejected, "只能改 Ambition"):
                self.cli(command, "<REDACTED_ID>", "comment-id", *args)
        self.assertEqual(mutations, [])
        self.assertEqual(remote["body"], "原文")
        self.assertFalse((Path(self.directory.name) / "deleted-comments.jsonl").exists())

    def test_edit_reads_back_exact_body_and_delete_backs_it_up(self):
        remote, mutations = self.setup_comment_api({"id": "ambition-id"})
        update = "新正文\n\n<!-- marker -->\n"
        self.cli("comment-edit", "<REDACTED_ID>", "comment-id", self.path(update))
        self.assertEqual(remote["body"], update)
        self.assertEqual(len(mutations), 1)
        remote["body"] = "原文"
        self.cli("comment-delete", "<REDACTED_ID>", "comment-id")
        self.assertFalse(remote)
        self.assertEqual(len(mutations), 2)

    def test_edit_rejects_a_mismatched_readback(self):
        self.setup_comment_api(None, bot={"name": "Ambition"}, tamper=True)
        with self.assertRaisesRegex(app.LinearError, "回读校验失败"):
            self.cli("comment-edit", "<REDACTED_ID>", "comment-id", self.path("新正文\n"))

    def test_read_only_rejects_state_mutation_with_alias_variables_and_comments(self):
        with patch.object(app, "gql") as remote:
            for query in [
                "mutation M($in:IssueUpdateInput!){x:issueUpdate(id:\"i\",input:$in){success}}",
                "# comment\nmutation{issueUpdate(id:\"i\",input:{stateId:\"new\"}){success}}",
                "query Q{viewer{id}} mutation X{a:issueUpdate(id:\"i\",input:{stateId:\"new\"}){success}}",
            ]:
                with self.subTest(query=query), self.assertRaisesRegex(app.LinearError, "只读"):
                    self.cli("gql", query, '{"in":{"stateId":"new"}}')
            remote.assert_not_called()
        app.read_only('query{viewer{id} issue(id:"mutation"){title}} # mutation')

    def test_nonempty_done_and_cancel_reason(self):
        api = self.setup_api()
        ident = api.card["identifier"]
        with self.assertRaisesRegex(app.LinearError, "文件"):
            self.cli("state", ident, "Done")
        with self.assertRaisesRegex(app.LinearError, "取消原因"):
            self.cli("state", ident, "Canceled")
        self.assertEqual(api.updates, [])
        self.cli("state", ident, "Canceled", "合并到 <REDACTED_ID>")
        self.assertTrue(api.card["description"].startswith("取消原因：合并到 <REDACTED_ID>"))
        self.assertEqual(api.card["state"]["name"], "Canceled")

    def test_explicit_legacy_approval_keeps_authentic_message_check_without_notifying(self):
        api = self.setup_api(long=True)
        ident = api.card["identifier"]
        result = self.path("目标已达成。")
        with self.assertRaisesRegex(app.LinearError, "ask"):
            self.cli("state", ident, "Human Intervention")
        self.cli("ask", ident, "- 验收证据 https://example.com/result", "--accept")
        record = app.read_record(api.card)
        self.assertEqual(api.card["state"]["name"], "In Progress")
        self.assertIn(record["requested_at"], api.card["description"])
        fake = app.LinearError("找不到这条用户消息")
        with patch.object(app, "owner_message", side_effect=fake), self.assertRaisesRegex(app.LinearError, "找不到"):
            self.cli("state", ident, "Done", result, "--owner-msg", "om_fake")
        requested = app.timestamp(record["requested_at"])
        received = requested + timedelta(seconds=1)
        approval = {"id": "om_true", "received_at": received.isoformat(),
                    "created_at": record["requested_at"], "text": app.approval_text(api.card, record["requested_at"])}
        with patch.object(app, "owner_message", return_value=approval), \
                patch.object(app, "datetime", wraps=app.datetime) as clock:
            clock.now.return_value = received + timedelta(seconds=1)
            with self.assertRaisesRegex(app.LinearError, "晚于"):
                self.cli("state", ident, "Done", result, "--owner-msg", "om_true")
            self.assertEqual(api.card["state"]["name"], "In Progress")
            approval["created_at"] = approval["received_at"]
            self.cli("state", ident, "Done", result, "--owner-msg", "om_true")
        self.assertEqual(api.card["state"]["name"], "Done")
        self.assertIn(app.quote(approval["text"]), api.card["description"])
        self.assertEqual(api.comments, [])
        self.assertEqual(app.read_record(api.card)["status"], "closed")

    def test_long_manual_owner_done_is_preserved(self):
        api = self.setup_api(long=True)
        api.card["state"] = {"id": app.STATES["Done"], "name": "Done"}
        api.history.append({"actor": {"id": app.OWNER["id"]}, "botActor": None,
                            "toStateId": app.STATES["Done"]})
        self.cli("audit", api.card["identifier"])
        with self.assertRaisesRegex(app.LinearError, "童成卓"):
            self.cli("state", api.card["identifier"], "In Progress")
        self.assertEqual(api.updates, [])

    def test_audit_preserves_long_card_manually_canceled_by_owner(self):
        api = self.setup_api(long=True)
        api.card["state"] = {"id": app.STATES["Canceled"], "name": "Canceled"}
        api.history.extend([
            {"actor": {"id": "other-user"}, "botActor": None, "toStateId": None},
            {"actor": {"id": app.OWNER["id"]}, "botActor": None, "toStateId": app.STATES["Canceled"]},
        ])
        record = {"status": "open", "requested_at": "2026-11-22T06:00:00Z"}
        app.save_record(api.card, record)
        with contextlib.redirect_stdout(StringIO()) as output:
            app.main(["audit", api.card["identifier"]])
        self.assertIn("Canceled", output.getvalue())
        self.assertEqual(api.updates, [])
        self.assertEqual(app.read_record(api.card), record)

    def test_state_rejects_changing_long_card_manually_canceled_by_owner(self):
        api = self.setup_api(long=True)
        api.card["state"] = {"id": app.STATES["Canceled"], "name": "Canceled"}
        api.history.append({"actor": {"id": app.OWNER["id"]}, "botActor": None,
                            "toStateId": app.STATES["Canceled"]})
        for args in (("In Progress",), ("Human Intervention",),
                     ("Done", self.path("目标已达成。"), "--owner-msg", "om_test")):
            with self.subTest(state=args[0]), self.assertRaisesRegex(app.LinearError, "童成卓.*亲手关闭"):
                self.cli("state", api.card["identifier"], *args)
        self.assertEqual(api.updates, [])

    def test_acceptance_request_cannot_reopen_long_card_manually_canceled_by_owner(self):
        api = self.setup_api(long=True)
        api.card["state"] = {"id": app.STATES["Canceled"], "name": "Canceled"}
        api.history.append({"actor": {"id": app.OWNER["id"]}, "botActor": None,
                            "toStateId": app.STATES["Canceled"]})
        with self.assertRaisesRegex(app.LinearError, "已取消"):
            self.cli("ask", api.card["identifier"], "请验收", "--accept")
        self.assertEqual(api.updates, [])
        self.assertEqual(api.comments, [])
        self.assertEqual(app.read_record(api.card), {})

    def test_audit_leaves_valid_long_card_states_alone(self):
        for state in ("Canceled", "Done", "Todo", "Human Intervention"):
            with self.subTest(state=state):
                api = self.setup_api(long=True)
                api.card["state"] = {"id": app.STATES[state], "name": state}
                self.cli("audit", api.card["identifier"])
                self.assertEqual(api.updates, [])

    def test_long_card_can_cancel_with_reason(self):
        api = self.setup_api(long=True)
        ident = api.card["identifier"]
        with self.assertRaisesRegex(app.LinearError, "--owner-msg"):
            self.cli("state", ident, "Canceled", "放弃", "--owner-msg", "om_test")
        self.cli("state", ident, "Canceled", "并入 <REDACTED_ID>")
        self.assertEqual(api.card["state"]["name"], "Canceled")
        self.assertTrue(api.card["description"].startswith("取消原因：并入 <REDACTED_ID>"))

    def test_closing_parent_does_not_close_live_children(self):
        api = self.setup_api()
        api.children = [{"identifier": "<REDACTED_ID>", "state": {"type": "started"}}]
        for args in (("Done", self.path("测试通过。")), ("Canceled", "不做了")):
            with self.assertRaisesRegex(app.WriteRejected, "<REDACTED_ID>"):
                self.cli("state", api.card["identifier"], *args)
        self.assertEqual(api.updates, [])

    def test_notification_defaults_and_explicit_ask(self):
        api = self.setup_api(long=True)
        ident = api.card["identifier"]
        self.cli("new", "核对测试结果", "--goal", "核对命令是否通过。")
        self.assertNotIn("subscriberIds", api.creates[-1])
        self.cli("comment", ident, "结果已经上卡。")
        self.assertNotIn(app.OWNER["url"], api.comments[-1]["body"])
        question = "请决定是当天上线还是下周上线（建议下周）。"
        self.cli("ask", ident, question)
        self.cli("ask", ident, question)
        self.assertEqual(api.card["state"]["name"], "In Progress")
        self.assertEqual(len(api.comments), 2)
        self.assertTrue(api.comments[-1]["body"].startswith(question))
        self.assertIn(app.OWNER["url"], api.comments[-1]["body"])
        self.cli("ask", ident, "请批准购买测试板。", "--blocking")
        self.assertEqual(api.card["state"]["name"], "Human Intervention")
        self.assertNotIn("assigneeId", api.updates[-1])
        self.assertEqual(len(api.creates), 1)

    def test_progress_only_replaces_progress_and_next_without_comments(self):
        api = self.setup_api(long=True)
        _, before = app.segments(api.card)
        ident = api.card["identifier"]
        self.cli("progress", ident, self.path("## 现在到哪了\n新现状\n\n## 下一步\n新下一步"))
        _, updated = app.segments(api.card)
        self.assertIn("新现状", updated["progress"])
        self.assertIn("新下一步", updated["next"])
        self.cli("progress", ident, self.path("机械标注"), "--stale")
        _, stale = app.segments(api.card)
        self.assertIn("机械标注", stale["progress"])
        self.assertEqual(stale["next"], updated["next"])
        self.assertEqual({k: stale[k] for k in ("goal", "words", "products")},
                         {k: before[k] for k in ("goal", "words", "products")})
        self.assertEqual(api.comments, [])

    def test_stale_skips_recent_external_write(self):
        api = self.setup_api()
        with contextlib.redirect_stdout(None):
            changed = app.apply_progress(api.card["identifier"], self.path("还在跑"), True,
                                         "2026-11-22T05:59:59Z")
        self.assertEqual(changed, f"fresh {api.card['identifier']} {api.card['updatedAt']}")
        self.assertEqual(api.updates, [])

    def test_tagged_brief_preserves_long_multiline_quote_without_internal_guidance(self):
        words = "“第一行\n" + "字" * 6500 + "\n最后一行”"
        brief = "这是一段背景。\n用户原话：\n" + words + "\n执行条件：内部说明"
        self.assertEqual(app.original_words(brief, None), words + "\n")
        self.assertEqual(app.original_words("用户原话（14:38）：\n甲\n乙\n材料：内部", None), "甲\n乙\n")

    def fake_uploads(self):
        uploaded = {}

        def upload(path):
            uploaded[path.name] = path.read_text()
            return "https://example.com/attachment"

        self.enterContext(patch.object(app, "upload", side_effect=upload))
        return uploaded

    def test_new_preserves_unbounded_original_words_in_fold_or_attachment(self):
        api = self.setup_api()
        uploaded = self.fake_uploads()
        for count in (40, 1400):
            text = "用户原话 J-1122-ABC\n" * count
            with self.subTest(length=len(text)):
                self.cli("new", "题目", "--goal", "保全原话", "--user-words-file", self.path(text), "--received-at", "2026-11-22T06:38:00Z")
                self.assertIn("14:38", api.card["description"])
                if count == 40:
                    self.assertIn("+++ 你的原话（全文）", api.card["description"])
                    self.assertIn(app.quote(text), api.card["description"])
                else:
                    name = "<REDACTED_ID>-你的原话（全文）.md"
                    self.assertEqual(uploaded[name], text)
                    self.assertIn(f"全文：[{name}](https://example.com/attachment)", api.card["description"])
                self.assertEqual(api.comments, [])

    def test_full_result_round_trips_even_when_single_paragraph_exceeds_description_limit(self):
        api = self.setup_api()
        uploaded = self.fake_uploads()
        content = "一整段 J-1122-ABC " * 2500 + "\n\n末尾不能丢。\n"
        self.cli("state", api.card["identifier"], "Done", self.path(content))
        self.assertEqual(api.card["state"]["name"], "Done")
        name = "<REDACTED_ID>-结果全文.md"
        self.assertEqual(uploaded, {name: app.humanize(content.strip())})
        self.assertIn(f"全文：[{name}](https://example.com/attachment)", api.card["description"])
        self.assertEqual(api.comments, [])

    def test_result_drops_full_result_fold_if_cleaned_text_matches_summary(self):
        api = self.setup_api()
        _, blocks = app.segments(api.card)
        blocks["full-result"] = app.fold("结果全文", app.quote("旧结论"))
        api.card["description"] = app.description("", blocks)
        body = '详情：/tmp/old.md\n\n已完成验收。\n\n{"summary":"已完成验收。","goal_met":true}\n'
        self.cli("state", api.card["identifier"], "Done", self.path(body))
        _, blocks = app.segments(api.card)
        self.assertEqual(blocks["progress"], "## 结果\n已完成验收。")
        self.assertNotIn("full-result", blocks)

    def test_description_verification_accepts_linear_markdown_round_trip(self):
        expected = ("<!-- amb:progress -->\n## 结果\n完成。\n\n"
                    "- 产物 [交付](/tmp/result)\n\n"
                    "> **结果：`ok` 已确认。**\n> ~/.config\n\n"
                    "| A | B |\n|---|---|\n| x | y |")
        actual = ("<!-- amb:progress -->\n\n## 结果\n\n完成。\n\n"
                  "* 产物 [交付](</tmp/result>)\n\n"
                  "> **结果：**`ok` **已确认。**\n> \\~/.config\n\n"
                  "| A | B |\n| -- | -- |\n| x | y |")
        with patch.object(app, "issue", return_value={"description": actual}) as read:
            app.verify("issue-id", expected)
            for tampered in (actual.replace("ok", "bad"), actual.replace("完成", "失败"),
                             actual.replace("/tmp/result", "/tmp/other"),
                             actual.replace("amb:progress", "amb:next")):
                read.return_value = {"description": tampered}
                with self.assertRaisesRegex(app.LinearError, "回读校验失败"):
                    app.verify("issue-id", expected)

    def test_acceptance_survives_markdown_heading_spacing(self):
        requested = "2026-11-22T06:38:00Z"
        card = {"description": f"<!-- amb:accepted -->\n\n## 验收申请\n\n申请时间：{requested}\n\n证据"}
        self.assertTrue(app.open_acceptance(card, {"status": "open", "requested_at": requested}))

    def test_description_mismatch_emits_event_and_prevents_done(self):
        api = self.setup_api()
        api.tamper = True
        with self.assertRaisesRegex(app.LinearError, "回读校验失败"):
            self.cli("state", api.card["identifier"], "Done", self.path("交付完成。"))
        self.assertEqual(api.card["state"]["name"], "In Progress")
        app.report_error.assert_called_once()

    def test_forged_local_owner_journal_is_rejected(self):
        journal = self.path('{"id":"om_fake","text":"yes","received_at":"2099-01-01T00:00:00Z"}')
        with patch.object(app, "OWNER_RECORD", journal), self.assertRaisesRegex(app.LinearError, "不可验证"):
            app.owner_message("om_fake")


def test_new_rejects_invalid_card_fields_before_calling_linear():
    for title, goal in (("未写目标", ""), ("字" * 17, "确认完成"), ("题目", "字" * 301)):
        with patch.object(app, "gql") as api, unittest.TestCase().assertRaises(app.WriteRejected):
            app.create_card(SimpleNamespace(title=title, goal=goal))
        api.assert_not_called()
