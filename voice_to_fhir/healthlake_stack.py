"""Optional HealthLake FHIR R4 datastore (empty, SigV4 auth).

Only created when no `existingHealthLakeDatastoreId` is given. Kept out of the app
stack so the app can be destroyed and redeployed without touching the datastore
(creation takes a long time, and the datastore is billed per hour while it exists).
"""

from aws_cdk import CfnOutput, RemovalPolicy, Stack, aws_healthlake as healthlake
from constructs import Construct


class HealthLakeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, prefix: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        datastore = healthlake.CfnFHIRDatastore(
            self,
            "Datastore",
            datastore_type_version="R4",
            datastore_name=f"{prefix}-fhir",
        )
        # Delete with the stack: an idle datastore still costs money every hour.
        datastore.apply_removal_policy(RemovalPolicy.DESTROY)

        self.datastore_id = datastore.attr_datastore_id
        CfnOutput(self, "DatastoreId", value=datastore.attr_datastore_id)
        CfnOutput(self, "DatastoreEndpoint", value=datastore.attr_datastore_endpoint)
