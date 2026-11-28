"""HTTP API with a Cognito JWT authorizer on every route and one router Lambda."""

from aws_cdk import (
    Duration,
    Stack,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_s3 as s3,
)
from constructs import Construct

from voice_to_fhir.components.pipeline import python_function

ROUTES = (
    ("/patients", apigw.HttpMethod.GET),
    ("/patients/{id}/summary", apigw.HttpMethod.GET),
    ("/patients/{id}/fhir", apigw.HttpMethod.GET),
    ("/patients/{id}/encounters/{encounterId}", apigw.HttpMethod.PUT),
    ("/samples", apigw.HttpMethod.GET),
    ("/process", apigw.HttpMethod.POST),
    ("/process/upload", apigw.HttpMethod.POST),
    ("/drafts", apigw.HttpMethod.GET),
    ("/drafts/{id}", apigw.HttpMethod.GET),
    ("/drafts/{id}/notes", apigw.HttpMethod.POST),
    ("/drafts/{id}/notes/{template}/use", apigw.HttpMethod.POST),
    ("/drafts/{id}/items", apigw.HttpMethod.POST),
    ("/drafts/{id}/items/{itemId}", apigw.HttpMethod.PUT),
    ("/drafts/{id}/reject", apigw.HttpMethod.POST),
    ("/drafts/{id}/approve", apigw.HttpMethod.POST),
)


class Api(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, prefix: str, allowed_origin: str,
                 user_pool: cognito.UserPool, user_pool_client: cognito.UserPoolClient,
                 bucket: s3.Bucket, table: dynamodb.TableV2, healthlake_datastore_id: str,
                 pipeline) -> None:
        super().__init__(scope, construct_id)
        stack = Stack.of(self)

        self.function = python_function(
            self, "ApiHandler",
            handler="api.handler", timeout=20, memory=512,
            environment={
                "DRAFTS_TABLE": table.table_name,
                "DATA_BUCKET": bucket.bucket_name,
                "HEALTHLAKE_DATASTORE_ID": healthlake_datastore_id,
                "JOB_PREFIX": prefix,
                "SCRIBE_ROLE_ARN": pipeline.scribe_role.role_arn,
                "WATCHER_FUNCTION": pipeline.scribe_watcher.function_name,
            },
        )
        fn = self.function

        # --- least-privilege IAM -------------------------------------------------------------
        table.grant(fn, "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Scan")
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:ListBucket"], resources=[bucket.bucket_arn],
            conditions={"StringLike": {"s3:prefix": ["samples/*", "notes/*"]}}))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            # audio/*: presigned playback links for the review page.
            resources=[bucket.arn_for_objects(p) for p in ("samples/*", "drafts/*", "nlp/*", "audio/*", "notes/*")]))
        fn.add_to_role_policy(iam.PolicyStatement(
            # audio/*: new drafts; drafts/*: clinician edits and review feedback.
            actions=["s3:PutObject"], resources=[bucket.arn_for_objects("audio/*"), bucket.arn_for_objects("drafts/*")]))
        fn.add_to_role_policy(iam.PolicyStatement(
            # CreateResource: POST on approve; UpdateResource: PUT of the encounter (client id).
            actions=["healthlake:SearchWithGet", "healthlake:ReadResource",
                     "healthlake:CreateResource", "healthlake:UpdateResource"],
            resources=[stack.format_arn(service="healthlake", resource="datastore",
                                        resource_name=f"fhir/{healthlake_datastore_id}")]))
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["transcribe:GetMedicalTranscriptionJob"],
            resources=[stack.format_arn(service="transcribe", resource="medical-transcription-job",
                                        resource_name=f"{prefix}-*")]))

        pipeline.grant_start_scribe(fn, bucket)  # "generate another note" re-runs HealthScribe

        # --- HTTP API ------------------------------------------------------------------------
        self.http_api = apigw.HttpApi(
            self,
            "HttpApi",
            api_name=f"{prefix}-api",
            create_default_stage=False,
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=[allowed_origin],
                allow_methods=[apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.POST, apigw.CorsHttpMethod.PUT],
                allow_headers=["authorization", "content-type"],
                max_age=Duration.hours(1),
            ),
        )
        self.http_api.add_stage(
            "DefaultStage",
            stage_name="$default",
            auto_deploy=True,
            throttle=apigw.ThrottleSettings(rate_limit=10, burst_limit=20),
        )
        authorizer = authorizers.HttpUserPoolAuthorizer(
            "CognitoJwt", user_pool, user_pool_clients=[user_pool_client])
        integration = integrations.HttpLambdaIntegration("ApiIntegration", fn)
        for path, method in ROUTES:
            self.http_api.add_routes(path=path, methods=[method], integration=integration, authorizer=authorizer)

        self.url = self.http_api.api_endpoint
