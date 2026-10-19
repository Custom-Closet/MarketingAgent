"""AWS CDK app: daily Lambda run of the marketing agent.

Deploy with `scripts/deploy.sh` (it builds the Lambda bundle first). Non-secret settings are
read from the project's .env; secrets live in Secrets Manager (`agent push-secrets`).
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_scheduler as scheduler,
    aws_scheduler_targets as targets,
    aws_secretsmanager as secretsmanager,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
)
from constructs import Construct
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
BUNDLE_DIR = ROOT / "build" / "lambda"
SECRET_NAME = "marketing-agent/secrets"

# Settings copied from .env into the Lambda environment. Secrets are deliberately excluded.
PASSTHROUGH_ENV = (
    "ANTHROPIC_MODEL", "ANTHROPIC_EFFORT", "LLM_PROVIDER", "BEDROCK_MODEL_ID", "BEDROCK_REGION", "SENDER_NAME", "SENDER_EMAIL", "PHYSICAL_ADDRESS",
    "DAILY_EMAIL_CAP", "MIN_DELAY_SECONDS", "MAX_DELAY_SECONDS", "FOLLOWUP_AFTER_DAYS", "MAX_FOLLOWUPS",
    "DIGEST_EMAIL", "DAILY_DRAFT_LIMIT", "DAILY_DISCOVERY_LIMIT", "IG_USER_ID", "IG_PAGE_ID", "GRAPH_API_VERSION",
)


class MarketingAgentStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, *, settings: dict[str, str], **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Shared SQLite database. Versioning doubles as a backup of every run.
        bucket = s3.Bucket(
            self, "Data",
            bucket_name=f"marketing-agent-data-{self.account}",
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
            lifecycle_rules=[s3.LifecycleRule(noncurrent_version_expiration=cdk.Duration.days(90))],
        )

        # Filled by `agent push-secrets`; CloudFormation only sets the initial empty value.
        secret = secretsmanager.Secret(
            self, "Secrets",
            secret_name=SECRET_NAME,
            secret_string_value=cdk.SecretValue.unsafe_plain_text("{}"),
        )

        env = {k: v for k, v in settings.items() if k in PASSTHROUGH_ENV and v}
        env.update({
            "AGENT_DB_S3_URI": f"s3://{bucket.bucket_name}/agent.db",
            "AGENT_SECRET_ID": secret.secret_name,
            "AGENT_DATA_DIR": "/tmp/agent",
            "BRAND_FILE": "/var/task/brand.md",
        })

        fn = lambda_.Function(
            self, "Daily",
            function_name="marketing-agent-daily",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="marketing_agent.lambda_handler.handler",
            code=lambda_.Code.from_asset(str(BUNDLE_DIR)),
            timeout=cdk.Duration.minutes(15),
            memory_size=1024,
            environment=env,
            retry_attempts=0,  # never re-run a partially completed send
            log_group=logs.LogGroup(self, "DailyLogs", retention=logs.RetentionDays.THREE_MONTHS,
                                    removal_policy=cdk.RemovalPolicy.DESTROY),
        )
        bucket.grant_read_write(fn)
        bucket.grant_delete(fn)  # lock object
        secret.grant_read(fn)

        scheduler.Schedule(
            self, "Morning",
            schedule_name="marketing-agent-daily",
            description="Daily marketing agent run, 08:00 India time",
            schedule=scheduler.ScheduleExpression.cron(minute="0", hour="8", time_zone=cdk.TimeZone.ASIA_KOLKATA),
            target=targets.LambdaInvoke(fn, retry_attempts=0),
        )

        # Email the team when a run fails or finishes with errors.
        alert_email = settings.get("ALERT_EMAIL") or settings.get("DIGEST_EMAIL") or settings.get("SENDER_EMAIL")
        if alert_email:
            topic = sns.Topic(self, "Alerts", display_name="Marketing Agent alerts")
            topic.add_subscription(subscriptions.EmailSubscription(alert_email))
            alarm = cloudwatch.Alarm(
                self, "DailyErrors",
                alarm_description="The daily marketing agent run failed. Check the Lambda logs.",
                metric=fn.metric_errors(period=cdk.Duration.hours(1), statistic="Sum"),
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            alarm.add_alarm_action(cw_actions.SnsAction(topic))

        cdk.CfnOutput(self, "DatabaseUri", value=f"s3://{bucket.bucket_name}/agent.db",
                      description="Set as AGENT_DB_S3_URI in your local .env")
        cdk.CfnOutput(self, "SecretName", value=secret.secret_name)
        cdk.CfnOutput(self, "FunctionName", value=fn.function_name)


app = cdk.App()
MarketingAgentStack(
    app, "MarketingAgent",
    settings={k: v or "" for k, v in dotenv_values(ROOT / ".env").items()},
    env=cdk.Environment(account=app.node.try_get_context("account") or None,
                        region=app.node.try_get_context("region") or "us-east-1"),
)
app.synth()
