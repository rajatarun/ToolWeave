"""Every function's errors reach someone, and its logs expire.

The server answers JSON-RPC errors itself, so Lambda's Errors metric stays at
0 while a tool fails on every call. What a failure does leave is an [ERROR]
line; these tests hold the wiring from every function's log group to the
alarm, including for a function added later. The functions had no declared
log group at all, so their logs were kept forever.
"""
from __future__ import annotations

import pathlib

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]


class _Loader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {suffix: loader.construct_sequence(node, deep=True)}
    return {suffix: loader.construct_mapping(node, deep=True)}


_Loader.add_multi_constructor("!", _tag)
T = yaml.load((REPO / "template.yaml").read_text(), Loader=_Loader)
R = T["Resources"]


def _of_type(kind):
    return {k: v for k, v in R.items() if v["Type"] == kind}


def test_every_log_group_feeds_the_error_metric():
    groups = set(_of_type("AWS::Logs::LogGroup"))
    fed = {v["Properties"]["LogGroupName"]["Ref"] for v in _of_type("AWS::Logs::MetricFilter").values()
           if v["Properties"]["MetricTransformations"][0]["MetricName"] == "ErrorLines"}
    assert groups and groups <= fed, sorted(groups - fed)


def test_every_function_logs_to_a_declared_group_that_expires():
    groups = _of_type("AWS::Logs::LogGroup")
    functions = {**_of_type("AWS::Serverless::Function"), **_of_type("AWS::Lambda::Function")}
    assert functions
    for name, fn in functions.items():
        ref = fn["Properties"]["LoggingConfig"]["LogGroup"]["Ref"]
        assert ref in groups, name
        assert groups[ref]["Properties"]["RetentionInDays"] <= 30, name


def test_the_filter_catches_logged_errors_timeouts_and_crashes():
    for f in _of_type("AWS::Logs::MetricFilter").values():
        for term in ('"[ERROR]"', '"Task timed out"', '"Runtime.ExitError"'):
            assert f"?{term}" in f["Properties"]["FilterPattern"]


def test_the_alarm_fires_on_the_first_error_line_and_notifies_the_topic():
    a = R["ErrorLinesAlarm"]["Properties"]
    assert a["MetricName"] == "ErrorLines" and a["Threshold"] == 1
    assert a["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    assert a["TreatMissingData"] == "notBreaching"
    assert a["AlarmActions"] == [{"Ref": "OpsAlertsTopic"}]


def test_email_is_optional_and_never_committed():
    assert T["Parameters"]["AlertEmail"]["Default"] == ""
    sub = R["OpsAlertsEmailSubscription"]
    assert sub["Condition"] == "HasAlertEmail"
    workflow = (REPO / ".github/workflows/deploy.yml").read_text()
    assert "ALERT_EMAIL: ${{ vars.ALERT_EMAIL }}" in workflow
    assert 'PARAM_OVERRIDES+=("AlertEmail=${ALERT_EMAIL}")' in workflow
