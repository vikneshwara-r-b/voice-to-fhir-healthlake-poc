"""CDK assertion tests for the app stack (M1 scope: auth, data, frontend hosting)."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from voice_to_fhir.app_stack import VoiceToFhirStack
from voice_to_fhir.components.frontend import render_config_js
from voice_to_fhir.config import AppConfig


def _synth(**context) -> Template:
    app = cdk.App(context={"environment": "test", **context})
    stack = VoiceToFhirStack(
        app,
        "TestStack",
        config=AppConfig.from_app(app),
        healthlake_datastore_id="0123456789abcdef0123456789abcdef",
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return _synth()


def test_all_buckets_are_private_encrypted_and_ssl_only(template):
    buckets = template.find_resources("AWS::S3::Bucket")
    assert len(buckets) == 2
    for props in (b["Properties"] for b in buckets.values()):
        assert props["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }
        assert props["BucketEncryption"]["ServerSideEncryptionConfiguration"]
        assert "WebsiteConfiguration" not in props

    policies = template.find_resources("AWS::S3::BucketPolicy")
    assert len(policies) == 2
    for policy in policies.values():
        statements = policy["Properties"]["PolicyDocument"]["Statement"]
        assert any(
            s["Effect"] == "Deny" and s.get("Condition") == {"Bool": {"aws:SecureTransport": "false"}}
            for s in statements
        )


def test_user_pool_has_no_self_sign_up(template):
    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {"AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True}},
    )


def test_spa_client_has_no_secret_and_password_auth(template):
    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {
            "GenerateSecret": False,
            "ExplicitAuthFlows": Match.array_with(["ALLOW_USER_PASSWORD_AUTH"]),
        },
    )


def test_password_is_a_noecho_parameter_without_default(template):
    param = template.to_json()["Parameters"]["DemoUserPassword"]
    assert param["NoEcho"] is True
    assert "Default" not in param


def test_password_from_context_never_appears_outside_the_parameter():
    secret = "Synthetic-Only-Pw1"
    tpl = _synth(demoUserPassword=secret).to_json()
    assert tpl["Parameters"]["DemoUserPassword"]["Default"] == secret
    without_params = {k: v for k, v in tpl.items() if k != "Parameters"}
    assert secret not in json.dumps(without_params)


def test_drafts_table_is_on_demand_keyed_by_draft_id(template):
    template.has_resource_properties(
        "AWS::DynamoDB::GlobalTable",
        {
            "BillingMode": "PAY_PER_REQUEST",
            "KeySchema": [{"AttributeName": "draftId", "KeyType": "HASH"}],
        },
    )


def test_cloudfront_uses_oac_and_https(template):
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": Match.object_like(
                {
                    "DefaultCacheBehavior": Match.object_like(
                        {"ViewerProtocolPolicy": "redirect-to-https"}
                    )
                }
            )
        },
    )


def test_csp_allows_only_self_and_cognito(template):
    policy = next(iter(template.find_resources("AWS::CloudFront::ResponseHeadersPolicy").values()))
    raw = policy["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"][
        "ContentSecurityPolicy"
    ]["ContentSecurityPolicy"]
    # Fn::Join once the data bucket host (a deploy-time value) is part of media-src.
    csp = raw if isinstance(raw, str) else "".join(p if isinstance(p, str) else "<token>" for p in raw["Fn::Join"][1])
    assert "script-src 'self'" in csp
    assert "https://cognito-idp.us-east-1.amazonaws.com" in csp
    assert "unsafe-inline" not in csp
    assert "media-src 'self' blob: https://<token>;" in csp  # the data bucket's regional host
    assert "RegionalDomainName" in json.dumps(raw)


def test_log_groups_keep_one_week(template):
    groups = template.find_resources("AWS::Logs::LogGroup")
    assert groups
    for group in groups.values():
        assert group["Properties"]["RetentionInDays"] == 7


def test_render_config_js_is_an_es_module():
    js = render_config_js({"region": "us-east-1", "apiUrl": ""})
    assert "export const CONFIG = Object.freeze({" in js
    assert '"region": "us-east-1"' in js


@pytest.mark.parametrize(
    "password, rule",
    [
        ("short1A", "at least 8"),
        ("alllowercase1", "uppercase"),
        ("ALLUPPERCASE1", "lowercase"),
        ("NoDigitsHere", "digit"),
        (" Leading-Space1", "spaces"),
    ],
)
def test_bad_context_password_fails_synth_with_the_broken_rule(password, rule):
    with pytest.raises(ValueError, match=rule):
        _synth(demoUserPassword=password)


def test_inline_create_user_code_compiles():
    from voice_to_fhir.components.auth import _CREATE_USER_CODE

    compile(_CREATE_USER_CODE, "index.py", "exec")


# --- M2: pipeline ------------------------------------------------------------------------------

def _notification_configs(template):
    [notifications] = template.find_resources("Custom::S3BucketNotifications").values()
    return notifications["Properties"]["NotificationConfiguration"]["LambdaFunctionConfigurations"]


def _filters(config):
    return {r["Name"]: r["Value"] for r in config["Filter"]["Key"]["FilterRules"]}


def _function_logical_id(template, handler):
    return next(k for k, v in template.find_resources("AWS::Lambda::Function").items()
                if v["Properties"].get("Handler") == handler)


def _role_statements(template, function_id):
    role_ref = template.to_json()["Resources"][function_id]["Properties"]["Role"]["Fn::GetAtt"][0]
    return [s for p in template.find_resources("AWS::IAM::Policy").values()
            if {"Ref": role_ref} in p["Properties"]["Roles"]
            for s in p["Properties"]["PolicyDocument"]["Statement"]]


def test_every_s3_notification_has_prefix_and_suffix(template):
    configs = _notification_configs(template)
    assert {(f["prefix"], f["suffix"]) for f in map(_filters, configs)} == {
        ("audio/", ".wav"), ("audio/", ".mp3"), ("audio/", ".flac"), ("audio/", ".m4a"),
        ("transcripts/", ".json"),
    }
    for config in configs:
        assert config["Events"] == ["s3:ObjectCreated:*"]


def test_no_lambda_writes_to_the_prefix_that_triggers_it(template):
    for config in _notification_configs(template):
        function_id = config["LambdaFunctionArn"]["Fn::GetAtt"][0]
        trigger = _filters(config)["prefix"]
        for statement in _role_statements(template, function_id):
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            if "s3:PutObject" in actions:
                resources = json.dumps(statement["Resource"])
                assert f"/{trigger}*" not in resources, f"{function_id} writes to its trigger prefix {trigger}"


def test_pipeline_lambdas_are_python312_with_short_timeouts(template):
    for handler, timeout in (("start_transcription.handler", 30), ("analyze_transcript.handler", 120)):
        props = template.to_json()["Resources"][_function_logical_id(template, handler)]["Properties"]
        assert props["Runtime"] == "python3.12"
        assert props["Timeout"] == timeout


def test_no_wildcard_actions_and_star_resources_only_where_aws_requires(template):
    # Actions with no resource-level permissions, plus statements CDK's own helpers add
    # (bucket notifications handler; BucketDeployment's CloudFront invalidation).
    star_ok = {
        "transcribe:StartMedicalTranscriptionJob", "transcribe:StartMedicalScribeJob",
        "comprehendmedical:DetectEntitiesV2", "comprehendmedical:InferICD10CM", "comprehendmedical:InferRxNorm",
        "s3:PutBucketNotification",
        "cloudfront:GetInvalidation", "cloudfront:CreateInvalidation",
    }
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            assert not any(a == "*" or a.endswith(":*") for a in actions), actions
            if statement["Resource"] == "*":
                assert set(actions) <= star_ok, actions


def test_healthlake_stack_is_r4_and_deleted_with_the_stack():
    from voice_to_fhir.healthlake_stack import HealthLakeStack

    stack = HealthLakeStack(cdk.App(), "HL", prefix="voice-to-fhir-test")
    tpl = Template.from_stack(stack)
    tpl.has_resource("AWS::HealthLake::FHIRDatastore", {
        "Properties": {"DatastoreTypeVersion": "R4", "DatastoreName": "voice-to-fhir-test-fhir"},
        "DeletionPolicy": "Delete",
    })


# --- M3: API -----------------------------------------------------------------------------------

def test_every_api_route_uses_the_cognito_jwt_authorizer(template):
    template.has_resource_properties("AWS::ApiGatewayV2::Authorizer", {
        "AuthorizerType": "JWT",
        "IdentitySource": ["$request.header.Authorization"],
    })
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    assert {r["Properties"]["RouteKey"] for r in routes.values()} == {
        "GET /patients", "GET /patients/{id}/summary", "GET /patients/{id}/fhir",
        "PUT /patients/{id}/encounters/{encounterId}", "GET /samples", "POST /process", "POST /process/upload", "GET /drafts", "GET /drafts/{id}",
        "POST /drafts/{id}/reject", "POST /drafts/{id}/approve",
        "POST /drafts/{id}/items", "PUT /drafts/{id}/items/{itemId}", "POST /drafts/{id}/notes",
        "POST /drafts/{id}/notes/{template}/use",
    }
    for route in routes.values():
        assert route["Properties"]["AuthorizationType"] == "JWT"
        assert "AuthorizerId" in route["Properties"]


def test_api_cors_allows_only_the_cloudfront_origin(template):
    [api] = template.find_resources("AWS::ApiGatewayV2::Api").values()
    origins = api["Properties"]["CorsConfiguration"]["AllowOrigins"]
    assert len(origins) == 1
    getatt = origins[0]["Fn::Join"][1][1]["Fn::GetAtt"]
    assert origins[0]["Fn::Join"][1][0] == "https://"
    assert getatt[0].startswith("FrontendDistribution") and getatt[1] == "DomainName"
    assert "*" not in json.dumps(origins)


def test_api_stage_is_throttled(template):
    template.has_resource_properties("AWS::ApiGatewayV2::Stage", {
        "StageName": "$default",
        "DefaultRouteSettings": {"ThrottlingBurstLimit": 20, "ThrottlingRateLimit": 10},
    })


def test_healthlake_permissions_are_scoped_to_the_datastore(template):
    found = False
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            if any(a.startswith("healthlake:") for a in actions):
                found = True
                assert set(actions) <= {"healthlake:SearchWithGet", "healthlake:ReadResource",
                                         "healthlake:CreateResource", "healthlake:UpdateResource"}
                resource = json.dumps(statement["Resource"])
                assert "datastore/fhir/0123456789abcdef0123456789abcdef" in resource
                assert "*" not in resource
    assert found


def test_config_js_gets_the_api_url(template):
    deployments = template.find_resources("Custom::CDKBucketDeployment")
    assert any("HttpApi" in json.dumps(d["Properties"].get("SourceMarkers", "")) for d in deployments.values())



def test_healthscribe_permissions_are_scoped(template):
    statements = [st for p in template.find_resources("AWS::IAM::Policy").values()
                  for st in p["Properties"]["PolicyDocument"]["Statement"]]
    starts = [st for st in statements if st["Action"] == "transcribe:StartMedicalScribeJob"]
    assert len(starts) == 2  # start_transcription and the API (another note)
    for st in starts:
        assert "transcribe:OutputBucketName" in st["Condition"]["StringEquals"]
    passes = [st for st in statements if st["Action"] == "iam:PassRole"]
    assert passes and all(st["Condition"] == {"StringEquals": {"iam:PassedToService": "transcribe.amazonaws.com"}}
                          and st["Resource"] != "*" for st in passes)


def test_scribe_role_is_assumable_only_by_transcribe_in_this_account(template):
    roles = template.find_resources("AWS::IAM::Role")
    [scribe_role] = [r for k, r in roles.items() if k.startswith("PipelineScribeDataAccessRole")]
    [statement] = scribe_role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert statement["Principal"] == {"Service": "transcribe.amazonaws.com"}
    assert statement["Condition"] == {"StringEquals": {"aws:SourceAccount": "111111111111"}}


def test_scribe_watcher_does_not_retry_and_has_long_timeout(template):
    functions = template.find_resources("AWS::Lambda::Function")
    [watcher] = [k for k, f in functions.items() if f["Properties"].get("Handler") == "scribe_watcher.handler"]
    assert functions[watcher]["Properties"]["Timeout"] == 900
    template.has_resource_properties("AWS::Lambda::EventInvokeConfig", {"MaximumRetryAttempts": 0})


def test_demo_user_display_name_is_set_and_updatable(template):
    template.has_resource_properties("AWS::CloudFormation::CustomResource", {"DisplayName": "John Smith"})
    statements = [st for p in template.find_resources("AWS::IAM::Policy").values()
                  for st in p["Properties"]["PolicyDocument"]["Statement"]]
    assert any("cognito-idp:AdminUpdateUserAttributes" in (st["Action"] if isinstance(st["Action"], list) else [st["Action"]])
               for st in statements)
