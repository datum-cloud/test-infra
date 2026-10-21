import copy
import unittest

from driver import (check_history_output, normalized, unchanged,
                    exercise_history_diff_restore)


class HelperGuardsTest(unittest.TestCase):
    def test_full_nested_snapshot_keeps_bookkeeping_and_precision(self):
        before = {"template": {"metadata": {"name": "t", "resourceVersion": "1"},
                               "spec": {"definitionJSON": "{\"n\":9007199254740993}"}},
                  "revisions": [{"metadata": {"name": "r2", "resourceVersion": "2"}},
                                {"metadata": {"name": "r1", "resourceVersion": "3"}}]}
        after = copy.deepcopy(before)
        after["revisions"].reverse()
        self.assertEqual(normalized(before), normalized(after))
        unchanged(lambda: after, before, "read")
        after["template"]["metadata"]["resourceVersion"] = "4"
        with self.assertRaisesRegex(RuntimeError, "changed persisted"):
            unchanged(lambda: after, before, "read")
        after["template"]["metadata"]["resourceVersion"] = "1"
        after["template"]["spec"]["definitionJSON"] = "{\"n\":9007199254740992}"
        with self.assertRaisesRegex(RuntimeError, "changed persisted"):
            unchanged(lambda: after, before, "read")

    def test_history_requires_exact_revision_sequence(self):
        revisions = [{"metadata": {"name": "r1", "uid": "revision-1"},
                      "spec": {"templateUID": "template-uid", "sequence": 1,
                               "definitionJSON": "{}", "digest": "sha256:one",
                               "effectiveActorUID": "customer", "acceptedAt": "2026-10-20T00:00:00Z"}},
                     {"metadata": {"name": "r2", "uid": "revision-2"},
                      "spec": {"templateUID": "template-uid", "sequence": 2,
                               "definitionJSON": "{\"x\":1}", "digest": "sha256:two",
                               "effectiveActorUID": "customer", "acceptedAt": "2026-10-20T00:00:01Z"}}]
        output = {"templateUID": "template-uid", "revisions": [
            {"name": "r1", "sequence": 1, "digest": "sha256:one",
             "actorUID": "customer", "acceptedAt": "2026-10-20T00:00:00Z"},
            {"name": "r2", "sequence": 2, "digest": "sha256:two",
             "actorUID": "customer", "acceptedAt": "2026-10-20T00:00:01Z"}]}
        check_history_output(output, "template-uid", revisions)
        output["revisions"][1]["digest"] = "sha256:wrong"
        with self.assertRaisesRegex(RuntimeError, "history differs"):
            check_history_output(output, "template-uid", revisions)

    def test_dry_run_side_effect_fails_before_actual_restore(self):
        template = {"metadata": {"name": "t", "uid": "template-uid",
                                 "resourceVersion": "1"},
                    "spec": {"definitionJSON": "{\"x\":2}", "digest": "sha256:two"},
                    "status": {"latestSequence": 2}}
        revisions = [{"metadata": {"name": "r1", "uid": "revision-1"},
                      "spec": {"templateUID": "template-uid", "sequence": 1,
                               "definitionJSON": "{\"x\":1}", "digest": "sha256:one",
                               "effectiveActorUID": "customer", "acceptedAt": "2026-10-20T00:00:00Z"}},
                     {"metadata": {"name": "r2", "uid": "revision-2"},
                      "spec": {"templateUID": "template-uid", "sequence": 2,
                               "definitionJSON": "{\"x\":2}", "digest": "sha256:two",
                               "effectiveActorUID": "customer", "acceptedAt": "2026-10-20T00:00:01Z"}}]
        state = {"template": template, "revisions": revisions}

        class SideEffectCLI:
            def run_json(self, *args):
                if args[1] == "history":
                    return {"templateUID": "template-uid", "revisions": [
                        {"name": r["metadata"]["name"], "sequence": r["spec"]["sequence"],
                         "digest": r["spec"]["digest"],
                         "actorUID": r["spec"]["effectiveActorUID"],
                         "acceptedAt": r["spec"]["acceptedAt"]} for r in revisions]}
                if args[1] == "restore":
                    state["template"]["spec"]["digest"] = "sha256:mutated"
                    return {"name": "t", "uid": "template-uid", "action": "restored",
                            "dryRun": True, "resourceVersion": "1"}
                raise AssertionError(args)

            def run(self, *args):
                return "--- current\n+++ selected revision\n- 2\n+ 1\n"

        with self.assertRaisesRegex(RuntimeError, "server dry-run restore changed"):
            exercise_history_diff_restore(SideEffectCLI(),
                                          lambda: copy.deepcopy(state),
                                          lambda _: self.fail("unexpected commit"))


if __name__ == "__main__":
    unittest.main()
