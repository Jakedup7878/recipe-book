import os

import aws_cdk as cdk

from pipeline_stack import RecipeBookPipelineStack
from recipe_stack import RecipeBookStack

app = cdk.App()
# Every Regional resource must live in the project's selected Region.
env = cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-east-2")

domain = app.node.try_get_context("domain") or {}
RecipeBookStack(
    app, "RecipeBook",
    env=env,
    domain=domain if domain.get("name") else None,
    description="Recipe Book web app: Cognito accounts, Python API, DynamoDB, CloudFront site",
)

github = app.node.try_get_context("github") or {}
RecipeBookPipelineStack(
    app, "RecipeBookPipeline",
    env=env,
    github_owner=github.get("owner", ""),
    github_repo=github.get("repo", ""),
    branch=github.get("branch", "main"),
    description="Recipe Book CI/CD: GitHub -> tests -> synth/diff -> approval -> deploy",
)

cdk.Tags.of(app).add("project", "recipe-book")
app.synth()
