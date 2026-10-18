"""
Offline regression coverage for resolve_target_row_via_full_inbox_traversal
(rachel_appfolio_inbox.py) - the targeted, deep, full-inbox row resolver
used for controlled QA, explicit management lookup, recovery/backfill, and
ownership resolution when a target conversation is not in AppFolio's
initially rendered row set.

Everything here is pure orchestration logic: read_rows_fn and scroll_step_fn
are injected fakes, so no Playwright/browser is ever touched. This is
deliberately separate from normal lightweight inbox polling, which must stay
a single fast pass over whatever is already rendered - these tests exist to
prove the *deep* traversal path (scroll/load-more, dedup, bounded retries,
semantic end conditions) behaves correctly without ever opening a
conversation or writing anything.
"""

import os
import unittest

for key in (
    "ANTHROPIC_API_KEY", "APPFOLIO_EMAIL", "APPFOLIO_PASSWORD",
    "RACHEL_GMAIL_USER", "RACHEL_GMAIL_PASSWORD", "RACHEL_EMAIL_PASSWORD",
):
    os.environ.setdefault(key, "deterministic-test-value")

import rachel_appfolio_inbox as inbox
from rachel_event_memory import inbox_selector_key


def row(**changes):
    base = {
        "thread_id": "list-thread-default",
        "source_message_id": "list-event-default",
        "href": "",
        "contact_name": "Jamie Tenant",
        "contact_role": "tenant",
        "list_detail": "Tenant · Oak 2",
        "assigned_user": "Client Owner",
        "property": "100 Oak Street",
        "unit": "2",
        "preview": "Please review the attached question",
        "timestamp": "August 12 10:00 AM",
        "unread": True,
    }
    base.update(changes)
    return base


def filler_rows(count, *, prefix="filler"):
    return [
        row(
            thread_id=f"list-thread-{prefix}-{i}",
            source_message_id=f"list-event-{prefix}-{i}",
            contact_name=f"Contact {prefix} {i}",
            list_detail=f"Tenant · Unit {i}",
            preview=f"Filler preview {i}",
        )
        for i in range(count)
    ]


class ScrollStep:
    """A canned sequence of scroll/load-more step results."""

    def __init__(self, steps):
        self._steps = list(steps)
        self.calls = 0

    def __call__(self, page):
        self.calls += 1
        if self._steps:
            return self._steps.pop(0)
        return {"action": "no_scroll_target", "scrolled": False}


class ReadRows:
    """A canned sequence of row batches, one per call."""

    def __init__(self, batches):
        self._batches = list(batches)
        self.calls = 0

    def __call__(self, page):
        self.calls += 1
        if self._batches:
            return self._batches.pop(0)
        return []


class TargetRowTraversalTests(unittest.TestCase):
    def test_target_in_initial_rows_needs_no_traversal(self):
        target = row()
        key = inbox_selector_key(target)
        read_rows = ReadRows([[target] + filler_rows(5)])
        scroll_step = ScrollStep([])

        result = inbox.resolve_target_row_via_full_inbox_traversal(
            page=None, target_selector_key=key,
            read_rows_fn=read_rows, scroll_step_fn=scroll_step,
        )

        self.assertTrue(result["target_found"])
        self.assertEqual(result["end_condition"], "target_found")
        self.assertEqual(result["target_match_count"], 1)
        self.assertEqual(result["iterations"], 0)
        self.assertEqual(scroll_step.calls, 0)
        self.assertEqual(result["initial_rows"], 6)
        self.assertEqual(result["matched_row"]["thread_id"], target["thread_id"])

    def test_target_below_initial_rows_found_after_one_scroll(self):
        target = row(thread_id="list-thread-target")
        key = inbox_selector_key(target)
        read_rows = ReadRows([
            filler_rows(21),
            filler_rows(5, prefix="page2") + [target],
        ])
        scroll_step = ScrollStep([{"action": "scrolled", "scrolled": True}])

        result = inbox.resolve_target_row_via_full_inbox_traversal(
            page=None, target_selector_key=key,
            read_rows_fn=read_rows, scroll_step_fn=scroll_step,
        )

        self.assertTrue(result["target_found"])
        self.assertEqual(result["iterations"], 1)
        self.assertEqual(result["end_condition"], "target_found")
        self.assertEqual(result["initial_rows"], 21)

    def test_target_found_only_after_several_pagination_steps(self):
        target = row(thread_id="list-thread-deep-page")
        key = inbox_selector_key(target)
        read_rows = ReadRows([
            filler_rows(20, prefix="p0"),
            filler_rows(20, prefix="p1"),
            filler_rows(20, prefix="p2"),
            filler_rows(5, prefix="p3") + [target],
        ])
        scroll_step = ScrollStep([
            {"action": "load_more_clicked", "load_more_present": True,
             "load_more_clicked": True},
            {"action": "load_more_clicked", "load_more_present": True,
             "load_more_clicked": True},
            {"action": "load_more_clicked", "load_more_present": True,
             "load_more_clicked": True},
        ])

        result = inbox.resolve_target_row_via_full_inbox_traversal(
            page=None, target_selector_key=key,
            read_rows_fn=read_rows, scroll_step_fn=scroll_step,
        )

        self.assertTrue(result["target_found"])
        self.assertEqual(result["iterations"], 3)
        self.assertEqual(scroll_step.calls, 3)

    def test_virtualized_rows_where_earlier_rows_disappear(self):
        target = row(thread_id="list-thread-virtualized")
        key = inbox_selector_key(target)
        # Each read only shows a moving window; earlier rows drop out of the
        # DOM as new ones scroll in, as a virtualized list would behave.
        read_rows = ReadRows([
            filler_rows(10, prefix="window0"),
            filler_rows(10, prefix="window1"),
            filler_rows(3, prefix="window2") + [target],
        ])
        scroll_step = ScrollStep([
            {"action": "scrolled", "scrolled": True},
            {"action": "scrolled", "scrolled": True},
        ])

        result = inbox.resolve_target_row_via_full_inbox_traversal(
            page=None, target_selector_key=key,
            read_rows_fn=read_rows, scroll_step_fn=scroll_step,
        )

        self.assertTrue(result["target_found"])
        # 10 + 10 + 4 unique rows accumulated across non-overlapping windows.
        self.assertEqual(result["unique_rows_seen"], 24)

    def test_duplicate_rows_seen_again_during_scroll_are_deduped(self):
        target = row(thread_id="list-thread-dedup-target")
        key = inbox_selector_key(target)
        overlap = filler_rows(5, prefix="overlap")
        read_rows = ReadRows([
            overlap,
            overlap + [target],  # same 5 rows reappear, plus the target
        ])
        scroll_step = ScrollStep([{"action": "scrolled", "scrolled": True}])

        result = inbox.resolve_target_row_via_full_inbox_traversal(
            page=None, target_selector_key=key,
            read_rows_fn=read_rows, scroll_step_fn=scroll_step,
        )

        self.assertTrue(result["target_found"])
        # 5 unique filler rows + the target, not 11.
        self.assertEqual(result["unique_rows_seen"], 6)

    def test_true_target_absent_reaches_clean_list_end(self):
        key = inbox_selector_key(row(thread_id="list-thread-never-present"))
        read_rows = ReadRows([
            filler_rows(20, prefix="p0"),
            filler_rows(20, prefix="p1"),
        ])
        scroll_step = ScrollStep([
            {"action": "scrolled", "scrolled": True},
            {"action": "no_scroll_target", "scrolled": False},
        ])

        result = inbox.resolve_target_row_via_full_inbox_traversal(
            page=None, target_selector_key=key,
            read_rows_fn=read_rows, scroll_step_fn=scroll_step,
        )

        self.assertFalse(result["target_found"])
        self.assertEqual(result["target_match_count"], 0)
        self.assertEqual(result["end_condition"], "list_end_no_scroll_target")
        self.assertIsNone(result["matched_row"])

    def test_no_progress_terminates_without_looping_forever(self):
        key = inbox_selector_key(row(thread_id="list-thread-unreachable"))
        same_batch = filler_rows(10, prefix="stuck")
        read_rows = ReadRows([same_batch] + [same_batch] * 50)
        # Scroll step keeps claiming progress even though nothing new loads -
        # a broken/looping AppFolio UI.
        scroll_step = ScrollStep(
            [{"action": "scrolled", "scrolled": True}] * 50
        )

        result = inbox.resolve_target_row_via_full_inbox_traversal(
            page=None, target_selector_key=key,
            read_rows_fn=read_rows, scroll_step_fn=scroll_step,
            stable_iterations_required=3, max_iterations=40,
        )

        self.assertFalse(result["target_found"])
        self.assertEqual(result["end_condition"], "stable_no_new_rows")
        self.assertEqual(result["iterations"], 3)
        self.assertLess(scroll_step.calls, 40)

    def test_bounded_max_iterations_stops_a_truly_endless_list(self):
        key = inbox_selector_key(row(thread_id="list-thread-unreachable-2"))

        def endless_rows(page):
            endless_rows.n += 1
            return filler_rows(5, prefix=f"endless{endless_rows.n}")

        endless_rows.n = 0

        scroll_step = ScrollStep(
            [{"action": "scrolled", "scrolled": True}] * 1000
        )

        result = inbox.resolve_target_row_via_full_inbox_traversal(
            page=None, target_selector_key=key,
            read_rows_fn=endless_rows, scroll_step_fn=scroll_step,
            max_iterations=15, stable_iterations_required=1000,
            max_seconds=1000,
        )

        self.assertFalse(result["target_found"])
        self.assertEqual(result["end_condition"], "max_iterations_reached")
        self.assertEqual(result["iterations"], 15)

    def test_ambiguous_duplicate_target_is_reported_not_guessed(self):
        # Two distinct rows that collide on every field the selector-key
        # hashes (only their thread_id/timestamp differ, which are excluded
        # from the hash) - a genuine content collision, not a bug.
        first = row(thread_id="list-thread-dup-a", timestamp="9:00 AM")
        second = row(thread_id="list-thread-dup-b", timestamp="10:00 AM")
        key = inbox_selector_key(first)
        self.assertEqual(key, inbox_selector_key(second))

        read_rows = ReadRows([[first, second]])
        scroll_step = ScrollStep([])

        result = inbox.resolve_target_row_via_full_inbox_traversal(
            page=None, target_selector_key=key,
            read_rows_fn=read_rows, scroll_step_fn=scroll_step,
        )

        self.assertFalse(result["target_found"])
        self.assertEqual(result["target_match_count"], 2)
        self.assertEqual(result["end_condition"], "target_ambiguous")
        self.assertIsNone(result["matched_row"])
        self.assertEqual(scroll_step.calls, 0)

    def test_zero_conversation_opens_and_writes_across_all_outcomes(self):
        target = row(thread_id="list-thread-zero-write-check")
        found_key = inbox_selector_key(target)
        absent_key = inbox_selector_key(
            row(thread_id="list-thread-absent-zero-write-check")
        )
        dup_a = row(thread_id="list-thread-dup-zw-a")
        dup_b = row(thread_id="list-thread-dup-zw-b")
        ambiguous_key = inbox_selector_key(dup_a)

        scenarios = [
            (found_key, ReadRows([[target]]), ScrollStep([])),
            (
                absent_key,
                ReadRows([filler_rows(3)]),
                ScrollStep([{"action": "no_scroll_target", "scrolled": False}]),
            ),
            (ambiguous_key, ReadRows([[dup_a, dup_b]]), ScrollStep([])),
        ]
        for target_key, read_rows, scroll_step in scenarios:
            with self.subTest(target_key=target_key):
                result = inbox.resolve_target_row_via_full_inbox_traversal(
                    page=None, target_selector_key=target_key,
                    read_rows_fn=read_rows, scroll_step_fn=scroll_step,
                )
                self.assertEqual(result["conversation_opens"], 0)
                self.assertEqual(result["writes"], 0)

    def test_invalid_target_selector_key_fails_closed_without_traversal(self):
        read_rows = ReadRows([[row()]])
        scroll_step = ScrollStep([])

        result = inbox.resolve_target_row_via_full_inbox_traversal(
            page=None, target_selector_key="not-a-valid-fingerprint",
            read_rows_fn=read_rows, scroll_step_fn=scroll_step,
        )

        self.assertFalse(result["target_found"])
        self.assertEqual(result["end_condition"], "invalid_target_selector_key")
        self.assertEqual(read_rows.calls, 0)
        self.assertEqual(scroll_step.calls, 0)


if __name__ == "__main__":
    unittest.main()
