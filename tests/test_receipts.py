"""Offline tests for the logic that decides what gets sent. Run: python3 -m unittest discover -s tests"""

import atexit
import importlib.util
import os
import plistlib
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

HOME = tempfile.mkdtemp(prefix="receipts-test-")
atexit.register(shutil.rmtree, HOME, True)
os.environ["RECEIPTS_HOME"] = HOME
for var in ("RECEIPTS_SEND_TO", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
    os.environ.pop(var, None)

spec = importlib.util.spec_from_file_location("receipts", Path(__file__).resolve().parent.parent / "scripts" / "receipts.py")
receipts = importlib.util.module_from_spec(spec)
sys.modules["receipts"] = receipts
spec.loader.exec_module(receipts)


class ReceiptsTest(unittest.TestCase):
    def setUp(self):
        for path in Path(HOME).rglob("*"):
            if path.is_file():
                path.unlink()
        receipts.write_json(receipts.CONFIG_PATH, {"send_to": "inbox@example.com", "auto_since": "2026-08"})

    def manifest(self, **dates):
        receipts.write_json(receipts.MANIFEST_PATH, {key.replace("_", "/", 1): {"date": date} for key, date in dates.items()})

    def pdf(self, provider, month, content=b"%PDF-1"):
        path = receipts.receipt_path(provider, month)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


class PendingTest(ReceiptsTest):
    def test_current_month_waits_for_usual_issue_day(self):
        self.manifest(**{"tmobile_2026-10": "2026-10-26", "classpass_2026-10": "2026-10-27"})
        log = {f"{p}/{m}": {} for p in ("tmobile", "classpass") for m in ("2026-08", "2026-09", "2026-10")}
        due, waiting = receipts.pending_receipts(log, datetime(2026, 11, 9))
        self.assertEqual(due, [])
        self.assertEqual(waiting, [("tmobile", "2026-11", 26), ("classpass", "2026-11", 27)])

        due, waiting = receipts.pending_receipts(log, datetime(2026, 11, 26))
        self.assertEqual(due, [("tmobile", "2026-11")])
        self.assertEqual(waiting, [("classpass", "2026-11", 27)])

    def test_past_months_are_due_and_unknown_issue_day_is_tried(self):
        due, waiting = receipts.pending_receipts({"tmobile/2026-08": {}}, datetime(2026, 9, 3))
        self.assertEqual(due, [("tmobile", "2026-09"), ("classpass", "2026-08"), ("classpass", "2026-09")])
        self.assertEqual(waiting, [])

    def test_issue_day_is_clamped_to_short_months(self):
        self.manifest(**{"classpass_2027-01": "2027-01-31"})
        receipts.write_json(receipts.CONFIG_PATH, {"auto_since": "2027-02", "auto_providers": ["classpass"]})
        self.assertEqual(receipts.pending_receipts({}, datetime(2027, 2, 27))[0], [])
        self.assertEqual(receipts.pending_receipts({}, datetime(2027, 2, 28))[0], [("classpass", "2027-02")])

    def test_skip_list_and_manual_providers(self):
        receipts.write_json(
            receipts.CONFIG_PATH,
            {"auto_since": "2026-10", "auto_providers": ["verizon", "tmobile"], "auto_skip": ["tmobile/2026-10"]},
        )
        due, _ = receipts.pending_receipts({}, datetime(2026, 11, 9))
        # Verizon can't say "not issued yet", so its current month is never attempted.
        self.assertEqual(due, [("verizon", "2026-10"), ("tmobile", "2026-11")])

    def test_not_issued_yet_is_only_normal_for_the_current_month(self):
        errors = {}
        problem = f"{receipts.TMOBILE_NO_BILL} some month yet"
        receipts._report(errors, "tmobile", receipts.this_month(), problem)
        self.assertEqual(errors, {})
        receipts._report(errors, "tmobile", "2020-01", problem)
        self.assertEqual(list(errors.values()), [["2020-01"]])


class DeliverTest(ReceiptsTest):
    def deliver(self, ready, **kwargs):
        errors = {}
        with mock.patch.object(receipts, "send_email") as send:
            sent, skipped = receipts.deliver(ready, errors, **kwargs)
        return sent, skipped, errors, send.call_count

    def test_sends_and_records(self):
        path = self.pdf("tmobile", "2026-10")
        sent, _, errors, calls = self.deliver([("tmobile", "2026-10", path, {"sha256": "a"})])
        self.assertEqual((sent, errors, calls), ([("tmobile", "2026-10")], {}, 1))
        self.assertIn("tmobile/2026-10", receipts.load_sent_log())

    def test_two_receipts_for_one_month_send_nothing(self):
        path = self.pdf("classpass", "2026-10")
        ready = [
            ("classpass", "2026-10", path, {"sha256": "a", "id": "1"}),
            ("classpass", "2026-10", path, {"sha256": "a", "id": "2"}),
            ("tmobile", "2026-10", self.pdf("tmobile", "2026-10", b"%PDF-2"), {"sha256": "b"}),
        ]
        sent, _, errors, calls = self.deliver(ready)
        self.assertEqual((sent, calls), ([("tmobile", "2026-10")], 1))
        self.assertEqual(list(errors.values()), [["2026-10"]])

    def test_already_sent_is_skipped_unless_forced(self):
        path = self.pdf("tmobile", "2026-10")
        ready = [("tmobile", "2026-10", path, {"sha256": "a"})]
        self.deliver(ready)
        self.assertEqual(self.deliver(ready)[1:], ([("tmobile", "2026-10")], {}, 0))
        self.assertEqual(self.deliver(ready, force=True)[3], 1)

    def test_same_receipt_under_another_month_is_blocked(self):
        self.deliver([("classpass", "2026-09", self.pdf("classpass", "2026-09"), {"sha256": "a", "id": "77"})])
        ready = [("classpass", "2026-10", self.pdf("classpass", "2026-10", b"%PDF-2"), {"sha256": "b", "id": "77"})]
        sent, _, errors, calls = self.deliver(ready)
        self.assertEqual((sent, calls), ([], 0))
        self.assertIn("BLOCKED", next(iter(errors))[1])


class StripeTest(ReceiptsTest):
    RECEIPT = (
        "<html><head><style>.a{color:red}</style><script>var expired = 1;</script></head><body>"
        "<h1>Receipt from ClassPass</h1><p>Receipt #1712-4057</p>"
        "<td>Amount paid</td><td>$79.00</td><td>Date paid</td><td>October 27, 2026 at 5:06:35 PM UTC</td>"
        "</body></html>"
    )

    def render(self, dom, months, pdf=b"%PDF-1.4 fake"):
        def chrome(*args):
            for arg in args:
                if arg.startswith("--print-to-pdf="):
                    Path(arg.split("=", 1)[1]).write_bytes(pdf)
            return dom.encode()

        with mock.patch.object(receipts, "headless_chrome", side_effect=chrome):
            return receipts.render_stripe_receipt("https://pay.stripe.com/receipts/payment/abc", months)

    def test_reads_the_receipt_and_files_it_under_its_paid_month(self):
        month, path, info = self.render(self.RECEIPT, None)
        self.assertEqual((month, path.name), ("2026-10", "classpass-receipt-2026-10.pdf"))
        self.assertEqual((info["id"], info["date"], info["amount"]), ("1712-4057", "2026-10-27", "$79.00"))
        self.assertEqual(path.read_bytes(), b"%PDF-1.4 fake")
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_wrong_month_is_refused(self):
        with self.assertRaisesRegex(receipts.ReceiptError, "belongs to October 2026"):
            self.render(self.RECEIPT, ["2026-09"])

    def test_error_page_is_never_saved_as_a_receipt(self):
        with self.assertRaisesRegex(receipts.ReceiptError, "didn't load as a Stripe receipt"):
            self.render("<html><body>This site can't be reached</body></html>", ["2026-10"])
        with self.assertRaisesRegex(receipts.ReceiptError, "expired"):
            self.render("<html><body>This link has expired</body></html>", ["2026-10"])
        self.assertFalse(receipts.receipt_path("classpass", "2026-10").exists())

    def test_non_pdf_output_is_refused(self):
        with self.assertRaisesRegex(receipts.ReceiptError, "empty or missing"):
            self.render(self.RECEIPT, None, pdf=b"")
        self.assertFalse(receipts.receipt_path("classpass", "2026-10").exists())


class SetupTest(ReceiptsTest):
    def dialog(self, *answers):
        results = [mock.Mock(returncode=code, stdout=out) for code, out in answers]

        def run(command, **kwargs):
            return results.pop(0) if command[0] == "osascript" else mock.Mock()

        with mock.patch.object(receipts.subprocess, "run", side_effect=run) as run:
            return receipts.app_password_dialog(), [call.args[0][0] for call in run.call_args_list]

    def test_dialog_strips_the_spaces_google_shows(self):
        self.assertEqual(self.dialog((0, "Save\nabcd efgh ijkl mnop\n"))[0], "abcdefghijklmnop")

    def test_dialog_cancel_and_create_one(self):
        self.assertEqual(self.dialog((1, ""))[0], "")
        password, commands = self.dialog((0, "Create one\n\n"), (0, "Save\nxyz\n"))
        self.assertEqual((password, commands), ("xyz", ["osascript", "open", "osascript"]))

    def test_claude_fallback_is_off_by_default(self):
        with mock.patch.object(receipts, "claude_binary", return_value="/usr/local/bin/claude"):
            self.assertFalse(receipts.claude_fallback_enabled())
            receipts.write_json(receipts.CONFIG_PATH, {"claude_fallback": True})
            self.assertTrue(receipts.claude_fallback_enabled())

    def test_plist_runs_auto_send_with_the_given_python(self):
        plist = plistlib.loads(receipts.build_plist("/usr/bin/python3", Path("/x/receipts.py"), None, Path("/x/log"), None))
        self.assertEqual(plist["ProgramArguments"], ["/usr/bin/python3", "/x/receipts.py", "auto", "--send"])
        self.assertNotIn("RECEIPTS_CLAUDE_BIN", plist["EnvironmentVariables"])


if __name__ == "__main__":
    unittest.main()
