"""Builds the app's stacks. Shared by app.py (deploy) and scripts/generate_architecture_diagram.py."""

import os

import aws_cdk as cdk

from voice_to_fhir.app_stack import VoiceToFhirStack
from voice_to_fhir.config import AppConfig
from voice_to_fhir.healthlake_stack import HealthLakeStack


def build_app(app: cdk.App) -> None:
    config = AppConfig.from_app(app)
    env = cdk.Environment(account=os.getenv("CDK_DEFAULT_ACCOUNT"), region=config.region)

    # Default path: reuse an existing datastore (-c existingHealthLakeDatastoreId=...).
    # Otherwise a separate, independently destroyable stack creates one (billed per hour).
    datastore_id = config.existing_healthlake_datastore_id
    if not datastore_id:
        healthlake_stack = HealthLakeStack(
            app,
            f"VoiceToFhirHealthLake-{config.environment}",
            prefix=config.prefix,
            env=env,
            description="Voice-to-FHIR POC: HealthLake FHIR R4 datastore (synthetic data only)",
        )
        datastore_id = healthlake_stack.datastore_id

    VoiceToFhirStack(
        app,
        f"VoiceToFhir-{config.environment}",
        config=config,
        healthlake_datastore_id=datastore_id,
        env=env,
        description="Voice-to-FHIR HealthLake POC (synthetic data only)",
    )

    cdk.Tags.of(app).add("project", "voice-to-fhir-healthlake-poc")
    cdk.Tags.of(app).add("data-classification", "synthetic")
