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


def test_table_indexes_match_what_the_api_queries():
    # backend/recipe_api/main.py queries these by name. CloudFormation can add only one
    # index per deploy, so a change here should add (or remove) a single index at a time.
    table = next(iter(template().find_resources("AWS::DynamoDB::GlobalTable").values()))["Properties"]
    indexes = {i["IndexName"]: [(k["AttributeName"], k["KeyType"]) for k in i["KeySchema"]]
               for i in table["GlobalSecondaryIndexes"]}
    assert indexes == {
        "gsi1": [("gsi1pk", "HASH"), ("gsi1sk", "RANGE")],
        "byAuthor": [("authorId", "HASH"), ("createdAt", "RANGE")],
    }


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


MAIL_DOMAIN = {**DOMAIN, "supportForwardToParameter": "/recipe-book/support-forward-to"}


def records_by_name_and_type(t):
    return {(r["Properties"]["Name"] if isinstance(r["Properties"]["Name"], str) else "<token>",
             r["Properties"]["Type"]): r["Properties"]
            for r in t.find_resources("AWS::Route53::RecordSet").values()}


def test_support_mail_adds_mail_records_and_keeps_the_site_records():
    t = template(MAIL_DOMAIN)
    records = records_by_name_and_type(t)
    for name in ("example.com.", "www.example.com."):  # the CloudFront aliases are unchanged
        assert {(name, "A"), (name, "AAAA")} <= records.keys()
    assert records[("example.com.", "MX")]["ResourceRecords"] == ["10 inbound-smtp.us-east-2.amazonaws.com"]
    assert records[("example.com.", "TXT")]["ResourceRecords"] == ['"v=spf1 include:amazonses.com ~all"']
    assert records[("_dmarc.example.com.", "TXT")]["ResourceRecords"] == [
        '"v=DMARC1; p=none; rua=mailto:dmarc-reports@example.com"']
    assert records[("mail.example.com.", "MX")]["ResourceRecords"] == ["10 feedback-smtp.us-east-2.amazonses.com"]
    assert records[("mail.example.com.", "TXT")]["ResourceRecords"] == ['"v=spf1 include:amazonses.com ~all"']
    dkim = [r for r in t.find_resources("AWS::Route53::RecordSet").values() if r["Properties"]["Type"] == "CNAME"]
    assert len(dkim) == 3  # Easy DKIM
    t.has_resource_properties("AWS::SES::EmailIdentity", {
        "EmailIdentity": "example.com",
        "MailFromAttributes": Match.object_like({"MailFromDomain": "mail.example.com"})})


def test_support_mail_is_stored_then_forwarded_and_the_rules_are_activated():
    t = template(MAIL_DOMAIN)
    rules = sorted((r["Properties"]["Rule"] for r in t.find_resources("AWS::SES::ReceiptRule").values()),
                   key=lambda rule: rule["Recipients"])
    assert [rule["Recipients"] for rule in rules] == [["dmarc-reports@example.com"], ["support@example.com"]]
    assert all(rule["ScanEnabled"] and rule["Enabled"] for rule in rules)
    dmarc, support = rules
    assert [list(action) for action in support["Actions"]] == [["S3Action"], ["LambdaAction"]]
    assert support["Actions"][0]["S3Action"]["ObjectKeyPrefix"] == "support/"
    assert support["Actions"][1]["LambdaAction"]["InvocationType"] == "Event"
    assert [list(action) for action in dmarc["Actions"]] == [["S3Action"]]
    activate = next(iter(t.find_resources("Custom::AWS").values()))["Properties"]
    assert "setActiveReceiptRuleSet" in str(activate["Create"]) and "setActiveReceiptRuleSet" in str(activate["Delete"])

    bucket = next(r for r in t.find_resources("AWS::S3::Bucket").values()
                  if "LifecycleConfiguration" in r["Properties"])
    assert bucket["DeletionPolicy"] == "Retain"
    assert bucket["Properties"]["LifecycleConfiguration"]["Rules"][0]["ExpirationInDays"] == 365


def test_forwarder_reads_the_address_from_parameter_store_at_run_time():
    t = template(MAIL_DOMAIN)
    forwarder = next(r for r in t.find_resources("AWS::Lambda::Function").values()
                     if r["Properties"].get("Handler") == "forwarder.handler")["Properties"]
    env = forwarder["Environment"]["Variables"]
    assert env["FORWARD_TO_PARAMETER"] == "/recipe-book/support-forward-to"
    assert env["FROM_ADDRESS"] == "support@example.com"
    assert forwarder["Runtime"] == "python3.13" and forwarder["Architectures"] == ["arm64"]
    # The only addresses in the template are the domain's own.
    assert "@" not in str(t.to_json()).replace("@example.com", "")
    t.has_resource_properties("AWS::IAM::Policy", {"PolicyDocument": Match.object_like({
        "Statement": Match.array_with([Match.object_like({
            "Action": "ssm:GetParameter",
            "Resource": {"Fn::Join": ["", Match.array_with([
                ":ssm:us-east-2:123456789012:parameter/recipe-book/support-forward-to"])]},
        })])})})


def test_support_mail_needs_its_parameter():
    t = template(DOMAIN)
    for resource_type in ("AWS::SES::EmailIdentity", "AWS::SES::ReceiptRuleSet", "Custom::AWS"):
        t.resource_count_is(resource_type, 0)
