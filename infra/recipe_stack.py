import shutil
import subprocess
import sys
from pathlib import Path

import jsii
from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    Duration,
    ILocalBundling,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_ses as ses,
    aws_ses_actions as ses_actions,
    custom_resources as cr,
)
from constructs import Construct

ROOT = Path(__file__).resolve().parent.parent
API_SOURCE = ROOT / "backend" / "recipe_api"
MAIL_FORWARDER_SOURCE = ROOT / "backend" / "mail_forwarder"
MAIL_RETENTION_DAYS = 365  # raw support mail and DMARC reports in the mail bucket
LAMBDA_RUNTIME = lambda_.Runtime.PYTHON_3_13
LAMBDA_PYTHON_VERSION = "3.13"


@jsii.implements(ILocalBundling)
class PipBundling:
    """Builds the Lambda package with the local pip, so Docker isn't needed.

    pydantic has compiled parts, so pip is told to fetch wheels built for
    Lambda's Linux ARM64 / Python 3.13 rather than for this computer. If pip
    can't do that, CDK falls back to bundling inside the Lambda Docker image.
    """

    def try_bundle(self, output_dir, *args, **kwargs) -> bool:
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade",
                 "-r", str(API_SOURCE / "requirements.txt"), "--target", output_dir,
                 "--platform", "manylinux2014_aarch64", "--implementation", "cp",
                 "--python-version", LAMBDA_PYTHON_VERSION, "--only-binary=:all:"],
                check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return False
        for source_file in API_SOURCE.glob("*.py"):
            shutil.copy2(source_file, output_dir)
        return True

# Overall API Gateway throttle (token bucket shared by all callers). Per-account
# and per-IP limits are enforced in the Lambda; see backend/recipe_api/ratelimit.py.
API_RATE_LIMIT = 50     # steady-state requests per second
API_BURST_LIMIT = 100   # short spikes

# (path, method, requires sign-in). Must match the FastAPI routes in
# backend/recipe_api/main.py; backend/tests/test_routes.py checks this.
ROUTES = [
    ("/public/recipes", "GET", False),
    ("/public/recipes/{id}", "GET", False),
    ("/public/recipes/{id}/comments", "GET", False),
    ("/me", "GET", True),
    ("/me", "PUT", True),
    ("/me/preferences", "PUT", True),
    ("/me/recipes", "GET", True),
    ("/recipes", "GET", True),
    ("/recipes", "POST", True),
    ("/recipes/{id}", "GET", True),
    ("/recipes/{id}", "PUT", True),
    ("/recipes/{id}", "DELETE", True),
    ("/recipes/{id}/sharing", "PUT", True),
    ("/recipes/{id}/comments", "GET", True),
    ("/recipes/{id}/comments", "POST", True),
    ("/recipes/{id}/comments/{commentId}", "DELETE", True),
    ("/families", "POST", True),
    ("/families/mine", "GET", True),
    ("/families/invite/{code}", "GET", True),
    ("/families/join", "POST", True),
    ("/families/{familyId}", "PUT", True),
    ("/families/{familyId}/recipes", "GET", True),
    ("/families/{familyId}/leave", "POST", True),
    ("/families/{familyId}/invite-code", "POST", True),
    ("/families/{familyId}/members/{userId}", "DELETE", True),
]


# Viewer-request CloudFront Function (not Lambda@Edge): sends www., the default
# *.cloudfront.net name and anything else to the one canonical domain.
CANONICAL_HOST_FUNCTION = """
function handler(event) {
  var request = event.request;
  var host = request.headers.host ? request.headers.host.value : "";
  if (host === "__DOMAIN__") {
    return request;
  }
  var query = Object.keys(request.querystring).map(function (key) {
    return key + "=" + request.querystring[key].value;
  }).join("&");
  return {
    statusCode: 301,
    statusDescription: "Moved Permanently",
    headers: { location: { value: "https://__DOMAIN__" + request.uri + (query ? "?" + query : "") } },
  };
}
"""


class RecipeBookStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, domain: dict | None = None, **kwargs) -> None:
        """`domain` (optional): {"name": "example.com", "certificateArn": ..., "hostedZoneId": ...}.

        The certificate must already be ISSUED in us-east-1 (CloudFront only uses certificates
        from there, and this project can't run CloudFormation in us-east-1) and must cover both
        the name and www.<name>. The hosted zone is the one Route 53 created for the domain.

        Optional `supportForwardToParameter`: the name of a Parameter Store String holding the
        inbox that support@<name> forwards to. With it, SES receives mail for the domain; see
        docs/operations.md. The parameter is created by hand so the address stays out of Git.
        """
        super().__init__(scope, construct_id, **kwargs)

        # The table and user pool hold real data (recipes, accounts), so they are RETAINed:
        # deleting the stack or a change that replaces them leaves the old one in place.
        # `cdk destroy` won't remove them; delete them by hand if you really mean to.
        # Everything else is rebuildable and is DESTROYed with the stack.

        table = dynamodb.TableV2(
            self, "Table",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            time_to_live_attribute="expiresAt",
            global_secondary_indexes=[
                # Feeds: PUBLIC, and one FAMILYFEED#<familyId> per family.
                dynamodb.GlobalSecondaryIndexPropsV2(
                    index_name="gsi1",
                    partition_key=dynamodb.Attribute(name="gsi1pk", type=dynamodb.AttributeType.STRING),
                    sort_key=dynamodb.Attribute(name="gsi1sk", type=dynamodb.AttributeType.STRING),
                ),
                # "My recipes". Keyed on attributes every recipe already has, so DynamoDB fills
                # it in for existing recipes when it's added (CloudFormation adds one index per
                # deploy). Add any further index in a deploy of its own.
                dynamodb.GlobalSecondaryIndexPropsV2(
                    index_name="byAuthor",
                    partition_key=dynamodb.Attribute(name="authorId", type=dynamodb.AttributeType.STRING),
                    sort_key=dynamodb.Attribute(name="createdAt", type=dynamodb.AttributeType.STRING),
                ),
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ---- Website: private S3 bucket served only through CloudFront ----
        site_bucket = s3.Bucket(
            self, "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        region = self.region
        headers = cloudfront.ResponseHeadersPolicy(
            self, "SecurityHeaders",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=(
                        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                        f"connect-src 'self' https://*.execute-api.{region}.amazonaws.com "
                        f"https://cognito-idp.{region}.amazonaws.com; "
                        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
                    ),
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN, override=True),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365), include_subdomains=True, override=True),
            ),
        )
        custom_domain = {}
        function_associations = []
        if domain:
            apex = domain["name"].strip().lower().rstrip(".")
            canonical_host = cloudfront.Function(
                self, "CanonicalHost",
                code=cloudfront.FunctionCode.from_inline(CANONICAL_HOST_FUNCTION.replace("__DOMAIN__", apex)),
                runtime=cloudfront.FunctionRuntime.JS_2_0,
                comment=f"Redirect every other host name to {apex}",
            )
            function_associations = [cloudfront.FunctionAssociation(
                function=canonical_host, event_type=cloudfront.FunctionEventType.VIEWER_REQUEST)]
            custom_domain = {
                "domain_names": [apex, f"www.{apex}"],
                "certificate": acm.Certificate.from_certificate_arn(self, "SiteCertificate",
                                                                    domain["certificateArn"]),
            }

        distribution = cloudfront.Distribution(
            self, "Site",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=headers,
                function_associations=function_associations,
            ),
            default_root_object="index.html",
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            **custom_domain,
        )

        if domain:
            zone = route53.PublicHostedZone.from_public_hosted_zone_attributes(
                self, "Zone", hosted_zone_id=domain["hostedZoneId"], zone_name=apex)
            target = route53.RecordTarget.from_alias(route53_targets.CloudFrontTarget(distribution))
            for record_id, record_name in (("Apex", None), ("Www", "www")):
                route53.ARecord(self, f"{record_id}A", zone=zone, record_name=record_name, target=target)
                route53.AaaaRecord(self, f"{record_id}Aaaa", zone=zone, record_name=record_name, target=target)
            site_host = apex
        else:
            site_host = distribution.distribution_domain_name
        site_url = f"https://{site_host}/"

        # ---- Accounts: Cognito user pool, used by the site's own sign-in pages ----
        # Keep the password policy in sync with backend/local_dev.py and frontend/app.js.
        user_pool = cognito.UserPool(
            self, "Users",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
                fullname=cognito.StandardAttribute(required=False, mutable=True)),  # the `name` claim
            password_policy=cognito.PasswordPolicy(
                min_length=10, require_lowercase=True, require_uppercase=True,
                require_digits=True, require_symbols=False),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            feature_plan=cognito.FeaturePlan.LITE,
            removal_policy=RemovalPolicy.RETAIN,
        )
        web_client = user_pool.add_client(
            "WebClient",
            generate_secret=False,  # browser apps cannot keep a secret
            # USER_PASSWORD_AUTH sends the password over HTTPS straight to Cognito (never to
            # our API), the same exposure as Cognito's hosted page. Refresh tokens stay enabled.
            auth_flows=cognito.AuthFlow(user_password=True),
            disable_o_auth=True,  # otherwise CDK enables default OAuth flows with an example.com callback
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
            id_token_validity=Duration.hours(1),
            access_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
        )

        # ---- Python API ----
        api_logs = logs.LogGroup(self, "ApiFunctionLogs",
                                 retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY)
        api_function = lambda_.Function(
            self, "ApiFunction",
            runtime=LAMBDA_RUNTIME,
            architecture=lambda_.Architecture.ARM_64,
            handler="main.handler",
            code=lambda_.Code.from_asset(
                str(API_SOURCE),
                exclude=["__pycache__", "*.pyc"],
                bundling=BundlingOptions(
                    image=LAMBDA_RUNTIME.bundling_image,
                    platform="linux/arm64",
                    local=PipBundling(),
                    command=["bash", "-c",
                             "pip install -r requirements.txt -t /asset-output && cp *.py /asset-output"],
                ),
            ),
            # FastAPI + pydantic take longer to import than plain Python; more memory
            # also means more CPU on Lambda, which roughly halves cold-start time.
            memory_size=512,
            timeout=Duration.seconds(10),
            environment={"TABLE_NAME": table.table_name},
            log_group=api_logs,
        )
        table.grant_read_write_data(api_function)

        http_api = apigw.HttpApi(
            self, "HttpApi",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=[f"https://{site_host}"],  # the only host the site is served from
                allow_methods=[apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.POST,
                               apigw.CorsHttpMethod.PUT, apigw.CorsHttpMethod.DELETE],
                allow_headers=["Authorization", "Content-Type"],
                expose_headers=["Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
                max_age=Duration.hours(1),
            ),
        )
        authorizer = authorizers.HttpUserPoolAuthorizer(
            "CognitoAuthorizer", user_pool, user_pool_clients=[web_client])
        integration = integrations.HttpLambdaIntegration("ApiIntegration", api_function)
        for path, method, signed_in in ROUTES:
            http_api.add_routes(
                path=path,
                methods=[apigw.HttpMethod(method)],
                integration=integration,
                authorizer=authorizer if signed_in else None,
            )

        access_logs = logs.LogGroup(self, "ApiAccessLogs",
                                    retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY)
        stage = http_api.default_stage.node.default_child
        stage.default_route_settings = apigw.CfnStage.RouteSettingsProperty(
            throttling_rate_limit=API_RATE_LIMIT, throttling_burst_limit=API_BURST_LIMIT)
        stage.access_log_settings = apigw.CfnStage.AccessLogSettingsProperty(
            destination_arn=access_logs.log_group_arn,
            format=(
                '{"requestId":"$context.requestId","ip":"$context.identity.sourceIp",'
                '"route":"$context.routeKey","status":"$context.status",'
                '"latencyMs":"$context.responseLatency","error":"$context.error.message",'
                '"authError":"$context.authorizer.error"}'
            ),
        )

        # ---- Upload the site plus a config.json with this deployment's endpoints ----
        s3deploy.BucketDeployment(
            self, "DeploySite",
            sources=[
                s3deploy.Source.asset(str(ROOT / "frontend")),
                s3deploy.Source.json_data("config.json", {
                    "apiUrl": http_api.api_endpoint,
                    "cognitoEndpoint": f"https://cognito-idp.{region}.amazonaws.com/",
                    "clientId": web_client.user_pool_client_id,
                }),
            ],
            destination_bucket=site_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        CfnOutput(self, "SiteUrl", value=site_url)
        CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)

        forward_to_parameter = (domain or {}).get("supportForwardToParameter")
        if domain and forward_to_parameter:
            self._support_mail(zone, apex, forward_to_parameter)

    def _support_mail(self, zone, apex, forward_to_parameter):
        """support@ is received by SES, kept in S3 and forwarded to the owner's inbox.

        Also does step 1 of docs/plans/email-with-ses.md (domain identity, DKIM, MAIL FROM,
        DMARC), which sending from the domain later needs too.
        """
        region = self.region
        support_address = f"support@{apex}"
        dmarc_address = f"dmarc-reports@{apex}"

        # Domain identity with Easy DKIM and a MAIL FROM subdomain: CDK adds the three DKIM
        # CNAMEs and the MAIL FROM MX and SPF records to the zone. SES only receives mail for
        # a verified domain, and forwarding from support@ needs DKIM to pass DMARC.
        ses.EmailIdentity(
            self, "MailDomain",
            identity=ses.Identity.public_hosted_zone(zone),
            mail_from_domain=f"mail.{apex}",
        )
        route53.MxRecord(self, "MailMx", zone=zone, values=[
            route53.MxRecordValue(host_name=f"inbound-smtp.{region}.amazonaws.com", priority=10)])
        # SES sends with mail.<apex> as the envelope sender, which has its own SPF record; this
        # one covers the bare domain. DMARC is report-only (p=none) until the reports look clean.
        route53.TxtRecord(self, "MailSpf", zone=zone, values=["v=spf1 include:amazonses.com ~all"])
        route53.TxtRecord(self, "MailDmarc", zone=zone, record_name="_dmarc",
                          values=[f"v=DMARC1; p=none; rua=mailto:{dmarc_address}"])

        # Raw messages are real correspondence (deletion requests, abuse reports), so RETAINed.
        mail_bucket = s3.Bucket(
            self, "MailBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(MAIL_RETENTION_DAYS))],
            removal_policy=RemovalPolicy.RETAIN,
        )

        forwarder_logs = logs.LogGroup(self, "MailForwarderLogs",
                                       retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY)
        forwarder = lambda_.Function(
            self, "MailForwarder",
            runtime=LAMBDA_RUNTIME,
            architecture=lambda_.Architecture.ARM_64,
            handler="forwarder.handler",
            code=lambda_.Code.from_asset(str(MAIL_FORWARDER_SOURCE), exclude=["__pycache__", "*.pyc"]),
            memory_size=512,  # a message with attachments is held in memory a few times over
            timeout=Duration.seconds(30),
            environment={
                "FROM_ADDRESS": support_address,
                "MAIL_BUCKET": mail_bucket.bucket_name,
                "MAIL_PREFIX": "support/",
                "FORWARD_TO_PARAMETER": forward_to_parameter,
            },
            log_group=forwarder_logs,
        )
        mail_bucket.grant_read(forwarder, "support/*")
        forwarder.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[self.format_arn(service="ssm", resource="parameter",
                                       resource_name=forward_to_parameter.lstrip("/"))]))
        # In the SES sandbox the recipient must be a verified identity as well as the sender,
        # and its address is only known at run time, so this covers the project's identities.
        forwarder.add_to_role_policy(iam.PolicyStatement(
            actions=["ses:SendEmail", "ses:SendRawEmail"],
            resources=[self.format_arn(service="ses", resource="identity", resource_name="*")]))

        # SES rejects mail for any address no rule names, so only these two receive mail.
        rule_set = ses.ReceiptRuleSet(self, "MailRules", rules=[
            ses.ReceiptRuleOptions(
                recipients=[support_address],
                scan_enabled=True,  # spam and virus verdicts for the forwarder
                actions=[
                    ses_actions.S3(bucket=mail_bucket, object_key_prefix="support/"),
                    ses_actions.Lambda(function=forwarder,
                                       invocation_type=ses_actions.LambdaInvocationType.EVENT),
                ],
            ),
            ses.ReceiptRuleOptions(
                recipients=[dmarc_address],
                scan_enabled=True,
                actions=[ses_actions.S3(bucket=mail_bucket, object_key_prefix="dmarc-reports/")],
            ),
        ])
        # CloudFormation can create a rule set but not make it the active one (one per Region),
        # so a custom resource does that, and deactivates it again if this is removed.
        cr.AwsCustomResource(
            self, "ActivateMailRules",
            on_update=cr.AwsSdkCall(  # also used on create
                service="SES", action="setActiveReceiptRuleSet",
                parameters={"RuleSetName": rule_set.receipt_rule_set_name},
                physical_resource_id=cr.PhysicalResourceId.of("active-receipt-rule-set"),
            ),
            on_delete=cr.AwsSdkCall(service="SES", action="setActiveReceiptRuleSet", parameters={}),
            policy=cr.AwsCustomResourcePolicy.from_statements([iam.PolicyStatement(
                actions=["ses:SetActiveReceiptRuleSet"], resources=["*"])]),  # no resource-level form
            install_latest_aws_sdk=False,
            log_group=logs.LogGroup(self, "ActivateMailRulesLogs", retention=logs.RetentionDays.ONE_MONTH,
                                    removal_policy=RemovalPolicy.DESTROY),
        )

        CfnOutput(self, "MailBucketName", value=mail_bucket.bucket_name)
