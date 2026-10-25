"""Shared helpers for tests and local development (not deployed)."""

import os
import sys

import boto3

API_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recipe_api")


def use_fake_aws(table_name):
    """Point boto3 at fake credentials and make the API package importable."""
    os.environ.update(TABLE_NAME=table_name, AWS_DEFAULT_REGION="us-east-2",
                      AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing")
    os.environ.pop("AWS_PROFILE", None)
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)


def create_table(table_name):
    """Create a table shaped like the one in infra/recipe_stack.py (call inside moto's mock_aws)."""
    boto3.client("dynamodb").create_table(
        TableName=table_name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("pk", "sk", "gsi1pk", "gsi1sk")],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[{
            "IndexName": "gsi1",
            "KeySchema": [{"AttributeName": "gsi1pk", "KeyType": "HASH"},
                          {"AttributeName": "gsi1sk", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
    )
