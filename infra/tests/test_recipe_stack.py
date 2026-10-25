"""Guards the app stack's data and its optional custom domain."""

import os
import sys

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from recipe_stack import RecipeBookStack  # noqa: E402

DOMAIN = {
    "name": "example.com",
    "certificateArn": "arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555",
    "hostedZoneId": "Z0123456789ABCDEFGHIJ",
}


def template(domain=None):
    # Skip bundling the Lambda (a pip download); these checks only read the template.
    app = cdk.App(context={"aws:cdk:bundling-stacks": []})
    stack = RecipeBookStack(app, "RecipeBook", domain=domain,
                            env=cdk.Environment(account="123456789012", region="us-east-2"))
    return Template.from_stack(stack)


def test_table_and_user_pool_are_retained():
    t = template()
    for resource_type in ("AWS::DynamoDB::GlobalTable", "AWS::Cognito::UserPool"):
        resources = t.find_resources(resource_type)
        assert len(resources) == 1, resource_type
        resource = next(iter(resources.values()))
        assert resource["DeletionPolicy"] == "Retain", resource_type
        assert resource["UpdateReplacePolicy"] == "Retain", resource_type


def test_without_a_domain_the_site_uses_the_cloudfront_address():
    t = template()
    t.resource_count_is("AWS::Route53::RecordSet", 0)
    t.resource_count_is("AWS::CloudFront::Function", 0)
    distribution = next(iter(t.find_resources("AWS::CloudFront::Distribution").values()))
    assert "Aliases" not in distribution["Properties"]["DistributionConfig"]


def test_custom_domain_serves_both_names_and_redirects_to_one():
    t = template(DOMAIN)
    t.has_resource_properties("AWS::CloudFront::Distribution", {"DistributionConfig": Match.object_like({
        "Aliases": ["example.com", "www.example.com"],
        "ViewerCertificate": Match.object_like({"AcmCertificateArn": DOMAIN["certificateArn"],
                                                "SslSupportMethod": "sni-only"}),
        "DefaultCacheBehavior": Match.object_like({"FunctionAssociations": [
            Match.object_like({"EventType": "viewer-request"})]}),
    })})
    function = next(iter(t.find_resources("AWS::CloudFront::Function").values()))["Properties"]
    assert function["FunctionConfig"]["Runtime"] == "cloudfront-js-2.0"
    assert 'host === "example.com"' in function["FunctionCode"]


def test_custom_domain_dns_records_and_cors():
    t = template(DOMAIN)
    records = t.find_resources("AWS::Route53::RecordSet")
    assert sorted((r["Properties"]["Name"], r["Properties"]["Type"]) for r in records.values()) == [
        ("example.com.", "A"), ("example.com.", "AAAA"), ("www.example.com.", "A"), ("www.example.com.", "AAAA")]
    assert all(r["Properties"]["HostedZoneId"] == DOMAIN["hostedZoneId"] for r in records.values())
    t.has_resource_properties("AWS::ApiGatewayV2::Api", {
        "CorsConfiguration": Match.object_like({"AllowOrigins": ["https://example.com"]})})
