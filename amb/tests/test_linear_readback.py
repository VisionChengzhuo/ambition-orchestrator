import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from io import BytesIO, StringIO
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

staged = Path(__file__).resolve().parents[1] / "linear-app"
path = staged if staged.is_file() else Path.home() / "bin" / "linear-app"
loader = SourceFileLoader("linear_app_readback", str(path))
app = module_from_spec(spec_from_loader(loader.name, loader))
loader.exec_module(app)


class LinearReadback(unittest.TestCase):
    def test_attach_accepts_csv_bom_and_section_spacing(self):
        url = "https://uploads.linear.app/workspace/asset/file"
        prefix = "<!-- amb:media -->\n## 直接看\n\n"
        expected = prefix + f"| \ufeffoffset |\n| --- |\n| 500000 |\n\n[试验.csv]({url})"
        actual = (prefix.replace("-->\n", "-->\n\n") +
                  f"| offset |\n| -- |\n| 500000 |\n\n[asset-id]({url})")
        with tempfile.TemporaryDirectory() as directory:
            csv_file = Path(directory) / "试验.csv"
            csv_file.write_text("offset\n500000\n", encoding="utf-8-sig")
            self.assertEqual(app.table_markdown(csv_file), "| offset |\n| --- |\n| 500000 |")
        with patch.object(app, "issue", return_value={"description": actual}) as read, \
             patch.object(app, "READBACK_DELAYS", (0,)):
            app.verify("issue-id", expected)
            for changed in (actual.replace("500000", "600000"), actual.replace("/file", "/other"),
                            actual.replace("[asset-id]", "![asset-id]"),
                            actual.replace("amb:media", "amb:products")):
                with self.subTest(changed=changed):
                    read.return_value = {"description": changed}
                    with self.assertRaises(app.ReadbackError):
                        app.verify("issue-id", expected)

    def test_attach_preserves_escaped_image_alt_text(self):
        url = "https://uploads.linear.app/workspace/asset/file"
        expected = f"<!-- amb:media -->\n\n## 直接看\n图片\n\n![_sheet_1.png]({url})"
        actual = expected.replace("看\n", "看\n\n").replace("![_", r"![\_")
        with patch.object(app, "issue", return_value={"description": actual}) as read, \
             patch.object(app, "READBACK_DELAYS", (0,)):
            app.verify("issue-id", expected)
            for changed in (actual.replace(r"\_sheet", "sheet"),
                            actual.replace("/file", "/other"),
                            actual.replace("图片", "照片")):
                with self.subTest(changed=changed):
                    read.return_value = {"description": changed}
                    with self.assertRaises(app.ReadbackError):
                        app.verify("issue-id", expected)

    def test_linear_merges_adjacent_lists_without_changing_items(self):
        expected = "* 一个\n- 两个\n\nhttps://example.com/example\n"
        actual = "* 一个\n\n* 两个\n\n[https://example.com/example](<https://example.com/example>)"
        rendered = app.rendered_description(expected)
        self.assertTrue(app.markdown_matches(expected, actual, rendered))
        for changed in (actual.replace("两个", "三个"), actual.replace("* 两个", "1. 两个"),
                        actual.replace("/example>)", "/other>)")):
            self.assertFalse(app.markdown_matches(expected, changed, rendered))

    def test_linear_tightens_quoted_ordered_list_with_nested_content(self):
        expected = "> 3) First\n> 4) Second\n>    - Child\n>\n> 5) Third"
        actual = "> 3. First\n> 4. Second\n>    * Child\n> 5. Third"
        rendered = app.rendered_description(expected)
        self.assertTrue(app.markdown_matches(expected, actual, rendered))
        for changed in (actual.replace("3.", "2."), actual.replace("    * Child", " * Child"),
                        actual.replace("    * Child", "    * Changed"),
                        actual.replace("Second", "Second\n>\n>    Added paragraph")):
            with self.subTest(changed=changed):
                self.assertFalse(app.markdown_matches(expected, changed, rendered))

    def test_table_autolinks_assignments_and_single_quoted_urls(self):
        assignment = "hostname=node.example.com"
        url = "https://example.com/#/tasks/42"
        expected = f"| Result | Command |\n| --- | --- |\n| {assignment} gpus=8 | run '{url}' --check |"
        actual = expected.replace(assignment, f"[{assignment}](<http://{assignment}>)")
        actual = actual.replace(url, f"[{url}](<{url}>)")
        rendered = app.rendered_description(expected)
        self.assertTrue(app.markdown_matches(expected, actual, rendered))
        for changed in (actual.replace("http://hostname=", "http://other="),
                        actual.replace("/tasks/42>", "/tasks/43>"),
                        actual.replace("gpus=8", "gpus=7"), actual.replace("'", "")):
            with self.subTest(changed=changed):
                self.assertFalse(app.markdown_matches(expected, changed, rendered))
        self.assertFalse(app.markdown_matches(actual, expected, app.rendered_description(actual)))

    def test_bare_host_autolink_before_comma(self):
        expected = "<REDACTED_PATH>{merge.py,export_pdf.sh,verify.py}（附说明）"
        actual = expected.replace("merge.py", "[merge.py](<http://merge.py>)")
        rendered = app.rendered_description(expected)
        self.assertTrue(app.markdown_matches(expected, actual, rendered))
        for changed in (actual.replace("http://merge.py", "http://other.py"),
                        actual.replace("export_pdf.sh,", "")):
            self.assertFalse(app.markdown_matches(expected, changed, rendered))

        url = "https://example.com/a"
        expected = f"{url},b"
        actual = f"[{url}](<{url}>),b"
        self.assertFalse(app.markdown_matches(expected, actual, app.rendered_description(expected)))

    def test_unicode_bare_host_autolink_preserves_destination(self):
        label = "claude-full→15721→api.example.com"
        href = "http://example.com"
        expected = f"> 经 {label} 完成。"
        actual = f"> 经 [{label}](<{href}>) 完成。"
        rendered = app.rendered_description(expected)
        self.assertTrue(app.markdown_matches(expected, actual, rendered))
        for changed in (actual.replace(".com>", ".org>"),
                        actual.replace("http://", "https://"),
                        actual.replace("15721→", "15722→"),
                        actual.replace(".com>", ".com/other>")):
            with self.subTest(changed=changed):
                self.assertFalse(app.markdown_matches(expected, changed, rendered))

    def test_autolink_can_end_before_an_escaped_quote_in_a_result(self):
        url = "https://example.com/result"
        expected = f'> {{"summary":"{url}。","goal_met":true,"goal_gap":""}}'
        label = f'{url}。","goal_met":true,"goal_gap":"'
        href = app.MARKDOWN.normalizeLink(label)
        actual = expected.replace(label, f"[{label}](<{href}>)")
        rendered = app.rendered_description(expected)
        self.assertTrue(app.markdown_matches(expected, actual, rendered))
        self.assertFalse(app.markdown_matches(
            expected, actual.replace(href, href.replace("/result", "/other")), rendered
        ))
        self.assertFalse(app.markdown_matches(
            expected, actual.replace('true', 'false'), rendered
        ))

    def test_table_comment_autolink_preserves_comment_destination(self):
        url = "<REDACTED_URL>"
        expected = f"| Evidence |\n| --- |\n| {url} |"
        actual = expected.replace(url, f"[<REDACTED_ID>]({url})")
        rendered = app.rendered_description(expected)
        self.assertTrue(app.markdown_matches(expected, actual, rendered))
        for changed in (actual.replace("d30945a3", "d30945a4"),
                        actual.replace("#comment-d30945a3", ""),
                        actual.replace("[<REDACTED_ID>]", "[<REDACTED_ID>]")):
            self.assertFalse(app.markdown_matches(expected, changed, rendered))

    def test_project_link_uses_shared_markdown_comparison_and_checks_link(self):
        url = "https://example.com/readback"
        expected = f"## 文档\n- [打开]({url})\n"
        actual = f"## 文档\n\n* [打开](<{url}>)"
        link = {"id": "link-id", "label": "文档", "url": url}
        def project(content, links):
            return {"project": {"content": content, "externalLinks": {"nodes": links}}}

        with tempfile.TemporaryDirectory() as directory:
            content_file = Path(directory) / "overview.md"
            content_file.write_text(expected, encoding="utf-8")
            args = ["project-link", "测试项目", url, "文档", "--content-file", str(content_file)]
            for content, links, error in ((actual, [link], None),
                                          (actual.replace("打开", "丢失"), [link], "项目概述"),
                                          (actual, [{**link, "label": "旧"}], "项目资源链接")):
                with self.subTest(error=error), patch.object(app, "D", directory), \
                     patch.object(app, "project_id", return_value="project-id"), \
                     patch.object(app, "save_board_resource") as save, \
                     patch.object(app, "gql", side_effect=[project("旧", [link]), project(content, links)]), \
                     patch.object(app, "READBACK_DELAYS", (0,)), \
                     patch.object(app, "report_error"), patch("sys.stdout", new=StringIO()):
                    if error:
                        with self.assertRaisesRegex(app.LinearError, error):
                            app.main(args)
                    else:
                        app.main(args)
                    save.assert_called_once_with("project", "project{id}", {"content": expected}, "project-id")

    def test_reversed_project_link_arguments_fail_before_api_call(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "D", directory), \
             patch.object(app, "gql") as gql, patch.object(app, "report_error") as report:
            with self.assertRaisesRegex(app.WriteRejected, "项目 URL 标签"):
                app.main(["project-link", "测试项目", "文档", "https://example.com/readback"])
            gql.assert_not_called()
            report.assert_not_called()

    def test_http_200_graphql_reason_reaches_event_without_credentials(self):
        credential = "<REDACTED_CREDENTIAL>"
        response = {"errors": [{
            "message": f'Argument Validation Error: url "label" <REDACTED_AUTH_SCHEME> {credential} token=abc123',
            "extensions": {"code": "INVALID_INPUT", "type": "invalidInput", "stack": credential},
        }]}
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "D", directory), \
             patch.object(app, "token", return_value=credential), \
             patch.object(app.urllib.request, "urlopen", return_value=BytesIO(json.dumps(response).encode())), \
             patch.dict(sys.modules, {"amb_events": SimpleNamespace(emit=Mock())}):
            with self.assertRaises(app.LinearAPIError):
                app.main(["project-link", "测试项目", "https://example.com/readback", "文档"])
            raw = (Path(directory) / "linear-events.jsonl").read_text()
        event = json.loads(raw)
        self.assertEqual(event["api"]["status"], 200)
        self.assertFalse(event["api"]["rate_limited"])
        reason, = event["api"]["graphql_errors"]
        self.assertEqual(reason["code"], "INVALID_INPUT")
        self.assertIn("Argument Validation Error", reason["message"])
        for value in (credential, "abc123", '"stack"'):
            self.assertNotIn(value, raw)
