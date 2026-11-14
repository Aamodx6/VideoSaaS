"""Step Functions definition shared by IaC and path tests."""


def definition(handlers):
    def task(name, result, next_state):
        return {"Type": "Task", "Resource": handlers[name], "ResultPath": result,
                "Next": next_state, "Retry": [{"ErrorEquals": ["States.TaskFailed"],
                    "IntervalSeconds": 3, "MaxAttempts": 2, "BackoffRate": 2}],
                "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": None, "Next": "HandleFailure"}]}

    states = {"StartAt": "Scan", "TimeoutSeconds": 900, "States": {
        "Scan": task("scan", "$.report", "CheckScan"),
        "CheckScan": {"Type": "Choice", "Choices": [
            {"And": [{"Variable": "$.report.skip", "IsPresent": True},
                     {"Variable": "$.report.skip", "BooleanEquals": True}], "Next": "Done"},
            {"And": [{"Variable": "$.report.pending", "IsPresent": True},
                     {"Variable": "$.report.pending", "BooleanEquals": True}], "Next": "WaitForScan"}],
            "Default": "StartManagedChecks"},
        "WaitForScan": {"Type": "Wait", "Seconds": 20, "Next": "Scan"},
        "StartManagedChecks": task("start", "$.jobs", "Poll"),
        "Poll": task("poll", "$.assessment", "CheckAssessment"),
        "CheckAssessment": {"Type": "Choice", "Choices": [
            {"And": [{"Variable": "$.assessment.pending", "IsPresent": True},
                     {"Variable": "$.assessment.pending", "BooleanEquals": True}], "Next": "WaitForAssessment"}],
            "Default": "Finish"},
        "WaitForAssessment": {"Type": "Wait", "Seconds": 20, "Next": "Poll"},
        "HandleFailure": {"Type": "Pass", "Result": {}, "ResultPath": "$.assessment", "Next": "Finish"},
        "Finish": task("finish", None, "Done"),
        "Done": {"Type": "Succeed"},
    }}
    del states["States"]["Finish"]["Catch"]
    return states
