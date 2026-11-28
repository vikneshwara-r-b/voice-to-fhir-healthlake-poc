"""Cognito user pool, SPA client and one demo clinician with a permanent password."""

from aws_cdk import (
    Annotations,
    CfnParameter,
    CustomResource,
    Duration,
    RemovalPolicy,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
)
from constructs import Construct

# Invoked directly by CloudFormation (ServiceToken = function ARN), so the inline
# code gets the `cfnresponse` helper. The password is never logged.
_CREATE_USER_CODE = """
import boto3
import cfnresponse


def handler(event, context):
    props = event["ResourceProperties"]
    if event["RequestType"] == "Delete":
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, props.get("Email"))
        return
    try:
        client = boto3.client("cognito-idp")
        pool_id, email, password = props["UserPoolId"], props["Email"], props["Password"]
        display_name = props.get("DisplayName") or "Demo Clinician"
        try:
            client.admin_create_user(
                UserPoolId=pool_id,
                Username=email,
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    {"Name": "email_verified", "Value": "true"},
                    {"Name": "name", "Value": display_name},
                ],
                MessageAction="SUPPRESS",
            )
        except client.exceptions.UsernameExistsException:
            pass
        # Keep the display name (the `name` attribute shown in the app header) in sync on updates too.
        client.admin_update_user_attributes(
            UserPoolId=pool_id, Username=email, UserAttributes=[{"Name": "name", "Value": display_name}]
        )
        # Permanent password: the demo user never sees NEW_PASSWORD_REQUIRED.
        client.admin_set_user_password(
            UserPoolId=pool_id, Username=email, Password=password, Permanent=True
        )
        print("demo user ready")
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, email)
    except client.exceptions.InvalidPasswordException as exc:
        # Cognito's message names the broken rule (e.g. "must have uppercase characters");
        # it never contains the password itself.
        detail = exc.response.get("Error", {}).get("Message", "")
        print("demo user setup failed: InvalidPasswordException")
        cfnresponse.send(
            event, context, cfnresponse.FAILED, {}, props.get("Email"),
            reason=f"DemoUserPassword rejected by Cognito: {detail}",
        )
    except Exception as exc:  # report the error type only
        print(f"demo user setup failed: {type(exc).__name__}")
        cfnresponse.send(
            event, context, cfnresponse.FAILED, {}, props.get("Email"),
            reason=f"demo user setup failed: {type(exc).__name__}",
        )
"""


PASSWORD_MIN_LENGTH = 8


def password_policy_problems(password: str) -> list[str]:
    """Mirror the user pool's password policy so a bad -c value fails at synth time."""
    checks = [
        (len(password) >= PASSWORD_MIN_LENGTH, f"at least {PASSWORD_MIN_LENGTH} characters"),
        (any(c.isascii() and c.isupper() for c in password), "an uppercase letter (A-Z)"),
        (any(c.isascii() and c.islower() for c in password), "a lowercase letter (a-z)"),
        (any(c.isascii() and c.isdigit() for c in password), "a digit (0-9)"),
        (password == password.strip(), "no leading or trailing spaces"),
    ]
    return [f"needs {rule}" for ok, rule in checks if not ok]


class Auth(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        prefix: str,
        demo_user_email: str,
        demo_user_name: str = "John Smith",
        demo_user_password: str | None,
    ) -> None:
        super().__init__(scope, construct_id)

        # Preferred: `cdk deploy --parameters DemoUserPassword=...` keeps the value
        # out of the template. `-c demoUserPassword=...` also works but becomes the
        # parameter's Default, which is stored in the synthesized template.
        param_kwargs = {}
        if demo_user_password:
            problems = password_policy_problems(demo_user_password)
            if problems:
                raise ValueError(
                    "demoUserPassword does not meet the Cognito policy: " + "; ".join(problems)
                )
            param_kwargs["default"] = demo_user_password
            Annotations.of(self).add_warning_v2(
                "voice-to-fhir:passwordInTemplate",
                "demoUserPassword was passed with -c; it is stored as the parameter Default "
                "in the synthesized template. Prefer --parameters DemoUserPassword=...",
            )
        self.password_param = CfnParameter(
            scope,
            "DemoUserPassword",
            type="String",
            no_echo=True,
            min_length=PASSWORD_MIN_LENGTH,
            description=(
                "Permanent password for the demo clinician: 8+ characters with an uppercase "
                "letter, a lowercase letter and a digit; no leading or trailing spaces"
            ),
            constraint_description="must be at least 8 characters",
            **param_kwargs,
        )

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{prefix}-users",
            feature_plan=cognito.FeaturePlan.LITE,
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True, username=False),
            sign_in_case_sensitive=False,
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=PASSWORD_MIN_LENGTH,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            mfa=cognito.Mfa.OFF,
            account_recovery=cognito.AccountRecovery.NONE,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.user_pool_client = self.user_pool.add_client(
            "SpaClient",
            user_pool_client_name=f"{prefix}-spa",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True),
            disable_o_auth=True,
            prevent_user_existence_errors=True,
            id_token_validity=Duration.hours(1),
            access_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.hours(12),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
        )

        role = iam.Role(
            self,
            "CreateUserRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        self.user_pool.grant(role, "cognito-idp:AdminCreateUser", "cognito-idp:AdminSetUserPassword",
                             "cognito-idp:AdminUpdateUserAttributes")

        log_group = logs.LogGroup(
            self,
            "CreateUserLogs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        log_group.grant_write(role)

        create_user_fn = lambda_.Function(
            self,
            "CreateUserFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(_CREATE_USER_CODE),
            timeout=Duration.seconds(30),
            memory_size=128,
            role=role,
            log_group=log_group,
        )

        demo_user = CustomResource(
            self,
            "DemoUser",
            service_token=create_user_fn.function_arn,
            properties={
                "UserPoolId": self.user_pool.user_pool_id,
                "Email": demo_user_email,
                "DisplayName": demo_user_name,
                "Password": self.password_param.value_as_string,
            },
        )
        demo_user.node.add_dependency(self.user_pool_client)
