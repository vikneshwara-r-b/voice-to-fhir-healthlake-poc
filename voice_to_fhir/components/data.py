"""The single private data bucket, the drafts status table and the bundled sample audio.

Bucket prefixes: samples/ (bundled audio), audio/{patientId}/{encounterId}/...,
transcripts/, nlp/, drafts/.
"""

from pathlib import Path

from aws_cdk import (
    Annotations,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
)
from constructs import Construct

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples" / "audio"


class Data(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, prefix: str) -> None:
        super().__init__(scope, construct_id)

        self.bucket = s3.Bucket(
            self,
            "DataBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,  # SSE-S3: Transcribe needs no KMS grants
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # Status per draft: UPLOADED, TRANSCRIBING, ANALYZING, PENDING_REVIEW,
        # APPROVED, REJECTED, WRITTEN, FAILED (+ errorCode, never PHI).
        self.drafts_table = dynamodb.TableV2(
            self,
            "DraftsTable",
            table_name=f"{prefix}-drafts",
            partition_key=dynamodb.Attribute(name="draftId", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # samples/audio/*.wav (from scripts/generate_sample_audio.py) -> s3://bucket/samples/.
        # No notification listens on samples/, so publishing them starts nothing.
        if any(SAMPLES_DIR.glob("*.wav")):
            s3_deployment.BucketDeployment(
                self,
                "DeploySamples",
                sources=[s3_deployment.Source.asset(str(SAMPLES_DIR), exclude=["*", "!*.wav"])],
                destination_bucket=self.bucket,
                destination_key_prefix="samples/",
                memory_limit=256,
                log_group=logs.LogGroup(
                    self,
                    "DeploySamplesLogs",
                    retention=logs.RetentionDays.ONE_WEEK,
                    removal_policy=RemovalPolicy.DESTROY,
                ),
            )
        else:
            Annotations.of(self).add_warning_v2(
                "voice-to-fhir:noSamples",
                "samples/audio/ has no .wav files; run scripts/generate_sample_audio.py before deploying.",
            )
