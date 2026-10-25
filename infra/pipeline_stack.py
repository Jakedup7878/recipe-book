"""CI/CD: GitHub -> Test -> Synth (+ diff) -> manual approval -> Deploy, in CodePipeline V2.

Deployed once by hand (`cdk deploy RecipeBookPipeline`); after that every push
to the branch runs it. Changes to this file need another manual deploy, since
the pipeline doesn't update itself.

Permissions are split so only the Deploy stage can change AWS: the Synth
project may assume only CDK's read-only lookup role (for `cdk diff`), and the
Deploy project may assume CDK's deploy and file-publishing roles. The CDK
bootstrap roles in this project and Region are the only roles either can use.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_codebuild as codebuild,
    aws_codeconnections as codeconnections,
    aws_codepipeline as codepipeline,
    aws_codepipeline_actions as actions,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3 as s3,
)
from constructs import Construct

# Amazon Linux 2023 ARM image with Python 3.13 (same as the Lambda) and Node 24.
BUILD_IMAGE = "aws/codebuild/amazonlinux-aarch64-standard:4.0"
CDK_QUALIFIER = "hnb659fds"  # the default `cdk bootstrap` qualifier


class RecipeBookPipelineStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, github_owner: str, github_repo: str,
                 branch: str = "main", **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Created as PENDING: someone must finish the GitHub authorization once in the
        # console (Developer Tools > Settings > Connections). No API can do that step.
        connection = codeconnections.CfnConnection(
            self, "GitHubConnection", connection_name="recipe-book-github", provider_type="GitHub")

        artifact_bucket = s3.Bucket(
            self, "Artifacts",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30))],  # old build outputs
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        def bootstrap_role(kind):
            return f"arn:{self.partition}:iam::{self.account}:role/cdk-{CDK_QUALIFIER}-{kind}-role-{self.account}-{self.region}"

        def project(name, buildspec, description, timeout_minutes, assumable_roles=()):
            log_group = logs.LogGroup(self, f"{name}Logs", retention=logs.RetentionDays.ONE_MONTH,
                                      removal_policy=RemovalPolicy.DESTROY)
            build = codebuild.PipelineProject(
                self, name,
                project_name=f"recipe-book-{name.lower()}",
                description=description,
                build_spec=codebuild.BuildSpec.from_source_filename(f"pipeline/{buildspec}"),
                environment=codebuild.BuildEnvironment(
                    build_image=codebuild.LinuxArmBuildImage.from_code_build_image_id(BUILD_IMAGE),
                    compute_type=codebuild.ComputeType.SMALL,
                ),
                timeout=Duration.minutes(timeout_minutes),
                logging=codebuild.LoggingOptions(cloud_watch=codebuild.CloudWatchLoggingOptions(log_group=log_group)),
            )
            if assumable_roles:
                build.add_to_role_policy(iam.PolicyStatement(
                    actions=["sts:AssumeRole"], resources=[bootstrap_role(kind) for kind in assumable_roles]))
            return build

        test_project = project("Test", "buildspec-test.yml", "Runs backend and infrastructure tests", 20)
        synth_project = project("Synth", "buildspec-synth.yml", "Builds the cloud assembly and prints cdk diff", 20,
                                assumable_roles=["lookup"])
        deploy_project = project("Deploy", "buildspec-deploy.yml", "Deploys the approved cloud assembly", 30,
                                 assumable_roles=["deploy", "file-publishing", "lookup"])

        source_output = codepipeline.Artifact("Source")
        synth_output = codepipeline.Artifact("SynthOutput")  # -> $CODEBUILD_SRC_DIR_SynthOutput in Deploy

        source_role = iam.Role(self, "SourceActionRole", assumed_by=iam.AccountRootPrincipal())
        # Newer connections use the codeconnections ARN prefix; allow both spellings of the action.
        source_role.add_to_policy(iam.PolicyStatement(
            actions=["codeconnections:UseConnection", "codestar-connections:UseConnection"],
            resources=[connection.attr_connection_arn]))
        source_action = actions.CodeStarConnectionsSourceAction(
            action_name="GitHub",
            owner=github_owner,
            repo=github_repo,
            branch=branch,
            connection_arn=connection.attr_connection_arn,
            output=source_output,
            role=source_role,
        )

        pipeline = codepipeline.Pipeline(
            self, "Pipeline",
            pipeline_name="recipe-book",
            pipeline_type=codepipeline.PipelineType.V2,
            execution_mode=codepipeline.ExecutionMode.QUEUED,
            artifact_bucket=artifact_bucket,
            stages=[
                codepipeline.StageProps(stage_name="Source", actions=[source_action]),
                codepipeline.StageProps(stage_name="Test", actions=[actions.CodeBuildAction(
                    action_name="Tests", project=test_project, input=source_output)]),
                codepipeline.StageProps(stage_name="Synth", actions=[actions.CodeBuildAction(
                    action_name="SynthAndDiff", project=synth_project, input=source_output,
                    outputs=[synth_output])]),
                codepipeline.StageProps(stage_name="Approve", actions=[actions.ManualApprovalAction(
                    action_name="ApproveDeploy",
                    additional_information=("Open the SynthAndDiff build log in the Synth stage and review the "
                                            "cdk diff, then approve to deploy RecipeBook to us-east-2."))]),
                codepipeline.StageProps(stage_name="Deploy", actions=[actions.CodeBuildAction(
                    action_name="DeployRecipeBook", project=deploy_project, input=source_output,
                    extra_inputs=[synth_output])]),
            ],
            triggers=[codepipeline.TriggerProps(
                provider_type=codepipeline.ProviderType.CODE_STAR_SOURCE_CONNECTION,
                git_configuration=codepipeline.GitConfiguration(
                    source_action=source_action,
                    push_filter=[codepipeline.GitPushFilter(branches_includes=[branch])],
                ),
            )],
        )

        CfnOutput(self, "ConnectionArn", value=connection.attr_connection_arn,
                  description="Finish the GitHub authorization for this connection in the console")
        CfnOutput(self, "PipelineUrl",
                  value=f"https://{self.region}.console.aws.amazon.com/codesuite/codepipeline/pipelines/"
                        f"{pipeline.pipeline_name}/view?region={self.region}")
