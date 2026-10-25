"""Guards the app stack's data: accounts and recipes must survive stack deletes and replacements."""

import os
import sys

import aws_cdk as cdk
from aws_cdk.assertions import Template

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from recipe_stack import RecipeBookStack  # noqa: E402


def template():
    # Skip bundling the Lambda (a pip download); these checks only read the template.
    app = cdk.App(context={"aws:cdk:bundling-stacks": []})
    stack = RecipeBookStack(app, "RecipeBook", env=cdk.Environment(account="123456789012", region="us-east-2"))
    return Template.from_stack(stack)


def test_table_and_user_pool_are_retained():
    t = template()
    for resource_type in ("AWS::DynamoDB::GlobalTable", "AWS::Cognito::UserPool"):
        resources = t.find_resources(resource_type)
        assert len(resources) == 1, resource_type
        resource = next(iter(resources.values()))
        assert resource["DeletionPolicy"] == "Retain", resource_type
        assert resource["UpdateReplacePolicy"] == "Retain", resource_type
