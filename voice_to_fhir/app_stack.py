"""The single application stack. Concerns are split into constructs, not stacks."""

from aws_cdk import CfnOutput, Stack
from constructs import Construct

from voice_to_fhir.components.api import Api
from voice_to_fhir.components.auth import Auth
from voice_to_fhir.components.data import Data
from voice_to_fhir.components.frontend import FrontendSite
from voice_to_fhir.components.pipeline import Pipeline
from voice_to_fhir.config import AppConfig


class VoiceToFhirStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, config: AppConfig,
                 healthlake_datastore_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        auth = Auth(
            self,
            "Auth",
            prefix=config.prefix,
            demo_user_email=config.demo_user_email,
            demo_user_name=config.demo_user_name,
            demo_user_password=config.demo_user_password,
        )
        data = Data(self, "Data", prefix=config.prefix)
        pipeline = Pipeline(
            self,
            "Pipeline",
            bucket=data.bucket,
            table=data.drafts_table,
            job_prefix=config.prefix,
        )

        cognito_endpoint = f"https://cognito-idp.{self.region}.amazonaws.com"
        site = FrontendSite(
            self,
            "Frontend",
            # The exact API URL would make the distribution depend on the API, whose CORS
            # origin is the distribution (a cycle), so the CSP allows this region's APIs.
            connect_src=[cognito_endpoint, f"https://*.execute-api.{self.region}.amazonaws.com"],
            # Exact data bucket host: the bucket does not depend on the distribution, so no cycle.
            media_src=[f"https://{data.bucket.bucket_regional_domain_name}"],
        )
        api = Api(
            self,
            "Api",
            prefix=config.prefix,
            allowed_origin=site.url,
            user_pool=auth.user_pool,
            user_pool_client=auth.user_pool_client,
            bucket=data.bucket,
            table=data.drafts_table,
            healthlake_datastore_id=healthlake_datastore_id,
            pipeline=pipeline,
        )
        site.deploy_site({
            "region": self.region,
            "userPoolId": auth.user_pool.user_pool_id,
            "userPoolClientId": auth.user_pool_client.user_pool_client_id,
            "cognitoEndpoint": cognito_endpoint,
            "apiUrl": api.url,
        })

        CfnOutput(self, "SiteUrl", value=site.url)
        CfnOutput(self, "UserPoolId", value=auth.user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=auth.user_pool_client.user_pool_client_id)
        CfnOutput(self, "DataBucketName", value=data.bucket.bucket_name)
        CfnOutput(self, "DraftsTableName", value=data.drafts_table.table_name)
        CfnOutput(self, "ApiUrl", value=api.url)
        CfnOutput(self, "HealthLakeDatastoreId", value=healthlake_datastore_id)
