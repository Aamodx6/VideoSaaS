import unittest

from moderation_states import definition
from moderation_policy import decision


def run_path(scan_results, poll_results, failed_task=None):
    graph = definition({name: name for name in ("scan", "start", "poll", "finish")})
    data, path = {}, []
    current = graph["StartAt"]
    scans, polls = iter(scan_results), iter(poll_results)

    def matches(rule):
        if "And" in rule:
            return all(matches(child) for child in rule["And"])
        fields = rule["Variable"].removeprefix("$.").split(".")
        value = data
        for field in fields:
            if not isinstance(value, dict) or field not in value:
                return rule.get("IsPresent") is False
            value = value[field]
        return rule.get("IsPresent") is True or value == rule["BooleanEquals"]

    for _ in range(40):
        path.append(current)
        state = graph["States"][current]
        if state["Type"] == "Succeed":
            return path, data
        if state["Type"] == "Choice":
            current = next((rule["Next"] for rule in state["Choices"] if matches(rule)), state["Default"])
        elif state["Type"] == "Task":
            task = state["Resource"]
            if task == failed_task:
                current = state["Catch"][0]["Next"]
                continue
            result = next(scans) if task == "scan" else next(polls) if task == "poll" else {}
            if state.get("ResultPath"):
                data[state["ResultPath"].removeprefix("$.")] = result
            current = state["Next"]
        else:
            if "ResultPath" in state:
                data[state["ResultPath"].removeprefix("$.")] = state["Result"]
            current = state["Next"]
    raise AssertionError("Unexpected state loop")


class StatePathTests(unittest.TestCase):
    def test_complete_path(self):
        clean = {"media_valid": True, "malware": "NO_THREATS_FOUND", "frame_score": 0, "audio_toxicity": 0}
        path, data = run_path([{"media_valid": True}], [{"pending": True}, clean])
        self.assertIn("WaitForAssessment", path)
        self.assertEqual(decision(data["assessment"], 65, 90), ("PASS", None))

    def test_scan_wait_and_skip(self):
        path, _ = run_path([{"pending": True}, {"skip": True}], [])
        self.assertEqual(path[-1], "Done")
        self.assertIn("WaitForScan", path)
        self.assertNotIn("StartManagedChecks", path)

    def test_failed_checks_flow_to_review(self):
        for task in ("scan", "start", "poll"):
            with self.subTest(task=task):
                path, data = run_path([{"media_valid": True}], [], failed_task=task)
                self.assertIn("HandleFailure", path)
                self.assertEqual(decision(data["assessment"], 65, 90), ("REVIEW", "VALIDATION_UNAVAILABLE"))

    def test_block_path_reaches_disposition(self):
        path, data = run_path([{"media_valid": False}], [{"media_valid": False}])
        self.assertIn("Finish", path)
        self.assertEqual(decision(data["assessment"], 65, 90), ("BLOCK", "INVALID_MEDIA"))
