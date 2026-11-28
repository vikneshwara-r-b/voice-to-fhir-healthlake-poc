"""Deploy-time settings read from CDK context (-c key=value) or cdk.json.

Secrets never live here or in cdk.json: the demo user's password is a NoEcho
CloudFormation parameter (see components/auth.py).
"""

import re
from dataclasses import dataclass

import aws_cdk as cdk

_ENV_NAME = re.compile(r"^[a-z0-9-]{1,16}$")


def _ctx(app: cdk.App, key: str, default: str = "") -> str:
    """Return a context value; blank values count as unset."""
    value = app.node.try_get_context(key)
    if value in (None, ""):
        return default
    return str(value)


@dataclass(frozen=True)
class AppConfig:
    environment: str
    region: str
    demo_user_email: str
    demo_user_name: str
    demo_user_password: str | None
    existing_healthlake_datastore_id: str | None

    @property
    def prefix(self) -> str:
        return f"voice-to-fhir-{self.environment}"

    @classmethod
    def from_app(cls, app: cdk.App) -> "AppConfig":
        environment = _ctx(app, "environment", "dev")
        if not _ENV_NAME.match(environment):
            raise ValueError("context 'environment' must match [a-z0-9-]{1,16}")
        # Accept the reference repo's spelling too, so an existing command line still works.
        datastore_id = _ctx(app, "existingHealthLakeDatastoreId") or _ctx(
            app, "existingHealthlakeDatastoreId"
        )
        return cls(
            environment=environment,
            region=_ctx(app, "region", "us-east-1"),
            demo_user_email=_ctx(app, "demoUserEmail", "clinician.demo@example.com"),
            demo_user_name=_ctx(app, "demoUserName", "John Smith"),
            demo_user_password=_ctx(app, "demoUserPassword") or None,
            existing_healthlake_datastore_id=datastore_id or None,
        )
