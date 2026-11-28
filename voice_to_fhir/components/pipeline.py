"""S3-event pipeline: audio/ -> start_transcription -> transcripts/*.json -> analyze_transcript.

No Lambda can re-trigger itself: each one reads from a prefix it never writes to,
and every notification has both a prefix and a suffix filter (Transcribe's
write-access check file is not .json, so it never triggers analyze_transcript).
"""

from pathlib import Path

from aws_cdk import (
    Duration,
    Stack,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_notifications as s3n,
)
from constructs import Construct

LAMBDA_DIR = Path(__file__).resolve().parents[2] / "lambdas"
AUDIO_EXTENSIONS = ("wav", "mp3", "flac", "m4a")  # keep in sync with lambdas/shared/keys.py


def python_function(scope: Construct, construct_id: str, *, handler: str, timeout: int,
                    memory: int, environment: dict[str, str], retry_attempts: int | None = None) -> lambda_.Function:
    """Python 3.12 Lambda from the shared lambdas/ asset (stdlib + boto3 only)."""
    log_group = logs.LogGroup(
        scope, f"{construct_id}Logs",
        retention=logs.RetentionDays.ONE_WEEK,
        removal_policy=RemovalPolicy.DESTROY,
    )
    return lambda_.Function(
        scope,
        construct_id,
        runtime=lambda_.Runtime.PYTHON_3_12,
        architecture=lambda_.Architecture.ARM_64,
        handler=handler,
        code=lambda_.Code.from_asset(str(LAMBDA_DIR), exclude=["**/__pycache__", "**/*.pyc"]),
        timeout=Duration.seconds(timeout),
        memory_size=memory,
        environment=environment,
        log_group=log_group,
        **({"retry_attempts": retry_attempts} if retry_attempts is not None else {}),
    )


class Pipeline(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, bucket: s3.Bucket,
                 table: dynamodb.TableV2, job_prefix: str, confidence_threshold: str = "0.80",
                 code_score_floor: str = "0.30") -> None:
        super().__init__(scope, construct_id)
        stack = Stack.of(self)
        scribe_prefix = f"{job_prefix}-scribe-"  # HealthScribe job names; also their output key prefix
        thresholds = {"CONFIDENCE_THRESHOLD": confidence_threshold, "CODE_SCORE_FLOOR": code_score_floor}

        # AWS HealthScribe reads the audio and writes its output with this role (DataAccessRoleArn).
        self.scribe_role = iam.Role(
            self, "ScribeDataAccessRole",
            assumed_by=iam.ServicePrincipal("transcribe.amazonaws.com",
                                            conditions={"StringEquals": {"aws:SourceAccount": stack.account}}),
            description="AWS HealthScribe data access: read audio/, write its own job output",
        )
        self.scribe_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:GetObject"], resources=[bucket.arn_for_objects("audio/*")]))
        self.scribe_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:PutObject"],
            resources=[bucket.arn_for_objects(f"{scribe_prefix}*"),
                       bucket.arn_for_objects(".write_access_check_file.temp")]))

        # Watches one HealthScribe job (async invoke, polls GetMedicalScribeJob), then processes it.
        self.scribe_watcher = python_function(
            self, "ScribeWatcher",
            handler="scribe_watcher.handler", timeout=900, memory=256, retry_attempts=0,
            environment={"DRAFTS_TABLE": table.table_name, "DATA_BUCKET": bucket.bucket_name, **thresholds},
        )
        watcher = self.scribe_watcher
        watcher.add_to_role_policy(iam.PolicyStatement(
            actions=["transcribe:GetMedicalScribeJob"],
            resources=[stack.format_arn(service="transcribe", resource="medical-scribe-job",
                                        resource_name=f"{scribe_prefix}*")]))
        watcher.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[bucket.arn_for_objects(f"{scribe_prefix}*"), bucket.arn_for_objects("results/*")]))
        watcher.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:PutObject"],
            resources=[bucket.arn_for_objects(p) for p in ("results/*", "notes/*", "nlp/*", "drafts/*")]))
        watcher.add_to_role_policy(iam.PolicyStatement(
            actions=["comprehendmedical:DetectEntitiesV2", "comprehendmedical:InferICD10CM",
                     "comprehendmedical:InferRxNorm"],
            resources=["*"]))
        table.grant(watcher, "dynamodb:UpdateItem", "dynamodb:GetItem")

        self.start_transcription = python_function(
            self, "StartTranscription",
            handler="start_transcription.handler", timeout=30, memory=256,
            environment={"DRAFTS_TABLE": table.table_name, "JOB_PREFIX": job_prefix,
                         "SCRIBE_ROLE_ARN": self.scribe_role.role_arn,
                         "WATCHER_FUNCTION": watcher.function_name},
        )
        self.analyze_transcript = python_function(
            self, "AnalyzeTranscript",
            handler="analyze_transcript.handler", timeout=120, memory=512,
            environment={"DRAFTS_TABLE": table.table_name, **thresholds},
        )

        # start_transcription: Transcribe Medical uses the caller's credentials to read the
        # audio and write the transcript (there is no DataAccessRoleArn for medical jobs).
        start = self.start_transcription
        start.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"], resources=[bucket.arn_for_objects("audio/*")]))
        start.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:PutObject"], resources=[bucket.arn_for_objects("transcripts/*")]))
        start.add_to_role_policy(iam.PolicyStatement(
            # No resource-level permissions for this action (Service Authorization Reference).
            actions=["transcribe:StartMedicalTranscriptionJob"], resources=["*"]))
        table.grant(start, "dynamodb:UpdateItem", "dynamodb:GetItem")
        self.grant_start_scribe(start, bucket)

        analyze = self.analyze_transcript
        analyze.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"], resources=[bucket.arn_for_objects("transcripts/*")]))
        analyze.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:PutObject"],
            resources=[bucket.arn_for_objects(p) for p in ("nlp/*", "drafts/*", "results/*")]))
        analyze.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"], resources=[bucket.arn_for_objects("results/*")]))
        analyze.add_to_role_policy(iam.PolicyStatement(
            # Comprehend Medical has no resource-level permissions.
            actions=["comprehendmedical:DetectEntitiesV2", "comprehendmedical:InferICD10CM",
                     "comprehendmedical:InferRxNorm"],
            resources=["*"]))
        table.grant(analyze, "dynamodb:UpdateItem")

        for ext in AUDIO_EXTENSIONS:
            bucket.add_event_notification(
                s3.EventType.OBJECT_CREATED,
                s3n.LambdaDestination(start),
                s3.NotificationKeyFilter(prefix="audio/", suffix=f".{ext}"),
            )
        bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(analyze),
            s3.NotificationKeyFilter(prefix="transcripts/", suffix=".json"),
        )

    def grant_start_scribe(self, fn: lambda_.Function, bucket: s3.Bucket) -> None:
        """Start HealthScribe jobs (only into our bucket), pass the data access role, start the watcher."""
        fn.add_to_role_policy(iam.PolicyStatement(
            # No resource-level permissions; the output-bucket condition key scopes it.
            actions=["transcribe:StartMedicalScribeJob"], resources=["*"],
            conditions={"StringEquals": {"transcribe:OutputBucketName": bucket.bucket_name}}))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["iam:PassRole"], resources=[self.scribe_role.role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "transcribe.amazonaws.com"}}))
        self.scribe_watcher.grant_invoke(fn)
