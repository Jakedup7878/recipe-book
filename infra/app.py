import os

import aws_cdk as cdk

from recipe_stack import RecipeBookStack

app = cdk.App()
RecipeBookStack(
    app, "RecipeBook",
    # Every Regional resource must live in the project's selected Region.
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-east-2"),
    description="Recipe Book web app: Cognito accounts, Python API, DynamoDB, CloudFront site",
)
cdk.Tags.of(app).add("project", "recipe-book")
app.synth()
