import unittest

from workflow import step_summary_preview


class StepSummaryTest(unittest.TestCase):
    def test_oversized_summary_keeps_findings_before_diff(self):
        findings = (
            b"## Release scan\n### Hard failures\n- **non-crates-io**: test failure\n\n"
            b"### Flags\n\n#### <code>process</code>\n- test flag\n\n### Release diff\n"
        )
        line = "- Changed dependency <code>crate</code>: 1.0.0 → 1.0.1\n".encode("utf-8")
        limit = 1 << 20
        report = findings + line * (limit // len(line) + 1) + b"- Last diff entry.\n"
        self.assertGreater(len(report), limit)
        self.assertLess(len(report.decode("utf-8")), limit)

        preview = step_summary_preview(report)
        self.assertLess(len(preview), limit)
        self.assertTrue(preview.startswith(findings + line))
        self.assertNotIn(b"- Last diff entry.", preview)
        prefix, notice = preview.rsplit(b"\n\n", 1)
        self.assertTrue(report.startswith(prefix + b"\n"))
        self.assertIn(b"truncated", notice)
        self.assertIn(b"`scan-summary` artifact", notice)

    def test_summary_below_limit_is_unchanged(self):
        heading = b"## Release scan\n### Hard failures\nNone.\n\n### Flags\nNone.\n\n### Release diff\n"
        report = heading + b"x" * ((1 << 20) - len(heading) - 2) + b"\n"
        self.assertEqual(step_summary_preview(report), report)
