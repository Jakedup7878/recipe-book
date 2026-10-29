"""Checks the CI/CD pipeline's shape and, above all, who can deploy."""

import json
import os
import sys

import aws_cdk as cdk
import yaml
from aws_cdk.assertions import Match, Template

INFRA_DIR = os.path.join(os.path.dirname(__file__), "..")
ROOT = os.path.join(INFRA_DIR, "..")
sys.path.insert(0, INFRA_DIR)

from pipeline_stack import BUILD_IMAGE, RecipeBookPipelineStack  # noqa: E402

ACCOUNT = "123456789012"


def template():
    app = cdk.App()
    stack = RecipeBookPipelineStack(app, "RecipeBookPipeline", github_owner="someone", github_repo="recipe-book",
                                    env=cdk.Environment(account=ACCOUNT, region="us-east-2"))
    return Template.from_stack(stack)


def role_policy_for_project(t, project_name):
    """All IAM statements attached to the service role of the named CodeBuild project."""
    project = next(p for p in t.find_resources("AWS::CodeBuild::Project").values()
                   if p["Properties"]["Name"] == project_name)
    role_id = project["Properties"]["ServiceRole"]["Fn::GetAtt"][0]
    statements = []
    for policy in t.find_resources("AWS::IAM::Policy").values():
        if {"Ref": role_id} in policy["Properties"]["Roles"]:
            statements += policy["Properties"]["PolicyDocument"]["Statement"]
    return statements


def assumable_roles(statements):
    roles = []
    for statement in statements:
        if statement["Action"] == "sts:AssumeRole":
            resources = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
            roles += [json.dumps(r) for r in resources]
    return roles


def test_stages_run_in_order_with_a_manual_gate_before_deploy():
    t = template()
    pipeline = next(iter(t.find_resources("AWS::CodePipeline::Pipeline").values()))["Properties"]
    assert pipeline["PipelineType"] == "V2" and pipeline["ExecutionMode"] == "QUEUED"
    assert [s["Name"] for s in pipeline["Stages"]] == ["Source", "Test", "Synth", "Approve", "Deploy"]
    approve = pipeline["Stages"][3]["Actions"][0]["ActionTypeId"]
    assert approve["Category"] == "Approval" and approve["Provider"] == "Manual"
    deploy_inputs = [a["Name"] for a in pipeline["Stages"][4]["Actions"][0]["InputArtifacts"]]
    assert deploy_inputs == ["Source", "SynthOutput"]  # deploys the assembly that was diffed


def test_only_the_deploy_stage_can_use_cdk_deploy_roles():
    t = template()
    synth = assumable_roles(role_policy_for_project(t, "recipe-book-synth"))
    deploy = assumable_roles(role_policy_for_project(t, "recipe-book-deploy"))
    test = assumable_roles(role_policy_for_project(t, "recipe-book-test"))
    assert len(synth) == 1 and "lookup-role" in synth[0]
    assert any("deploy-role" in r for r in deploy) and any("file-publishing-role" in r for r in deploy)
    assert test == []
    assert all("hnb659fds" in r for r in synth + deploy)  # only CDK bootstrap roles


def test_builds_use_the_arm_image_and_files_in_the_repo():
    t = template()
    t.resource_count_is("AWS::CodeBuild::Project", 3)
    t.all_resources_properties("AWS::CodeBuild::Project", {
        "Environment": Match.object_like({"Image": BUILD_IMAGE, "Type": "ARM_CONTAINER",
                                          "ComputeType": "BUILD_GENERAL1_SMALL"}),
    })
    t.has_resource_properties("AWS::CodeConnections::Connection", {"ProviderType": "GitHub"})


def test_buildspec_files_exist_and_are_valid_yaml():
    for name in ("buildspec-test.yml", "buildspec-synth.yml", "buildspec-deploy.yml"):
        with open(os.path.join(ROOT, "pipeline", name), encoding="utf-8") as handle:
            spec = yaml.safe_load(handle)
        assert spec["version"] == 0.2 and spec["phases"]["build"]["commands"], name


def test_test_stage_runs_the_security_scans_and_fails_on_findings():
    # The scans gate every deploy; a deleted command, or one made to always pass, would
    # remove that gate without anything else noticing.
    with open(os.path.join(ROOT, "pipeline", "buildspec-test.yml"), encoding="utf-8") as handle:
        commands = yaml.safe_load(handle)["phases"]["build"]["commands"]
    scans = [c for c in commands if "pip_audit" in c or "bandit" in c]
    assert any("pip_audit -r backend/recipe_api/requirements.txt" in c for c in scans)
    assert any("bandit -r backend/recipe_api backend/mail_forwarder" in c for c in scans)
    assert not any("||" in c or "--exit-zero" in c for c in scans)
    with open(os.path.join(ROOT, "backend", "requirements-dev.txt"), encoding="utf-8") as handle:
        pinned = {line.split("==")[0] for line in handle.read().splitlines() if "==" in line}
    assert {"pip-audit", "bandit"} <= pinned
