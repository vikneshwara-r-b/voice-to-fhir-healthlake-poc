"""Static Bootstrap UI: private S3 bucket behind CloudFront (OAC).

config.js is generated here at synth time and its CDK tokens (pool ids, API URL)
are resolved by BucketDeployment at deploy time.
"""

import json
from pathlib import Path

from aws_cdk import (
    Duration,
    RemovalPolicy,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
)
from constructs import Construct

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
CONFIG_JS_KEY = "js/config.js"


def render_config_js(values: dict[str, str]) -> str:
    return (
        "// Generated at deploy time by voice_to_fhir/components/frontend.py.\n"
        f"export const CONFIG = Object.freeze({json.dumps(values, indent=2)});\n"
    )


class FrontendSite(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        connect_src: list[str],
        media_src: list[str],
    ) -> None:
        super().__init__(scope, construct_id)

        self.bucket = bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        csp = "; ".join(
            [
                "default-src 'self'",
                "script-src 'self'",
                "style-src 'self'",
                # Bootstrap's CSS uses data: SVGs for form controls and the navbar toggler.
                "img-src 'self' data:",
                "connect-src " + " ".join(["'self'", *connect_src]),
                # Presigned audio links from the data bucket; blob: previews a file before upload.
                "media-src " + " ".join(["'self'", "blob:", *media_src]),
                "object-src 'none'",
                "base-uri 'none'",
                "form-action 'none'",
                "frame-ancestors 'none'",
            ]
        )
        headers = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=csp, override=True
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER, override=True
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365), override=True
                ),
            ),
        )

        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=headers,
            ),
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
        )
        self.url = f"https://{self.distribution.distribution_domain_name}"

    def deploy_site(self, config_values: dict[str, str]) -> None:
        """Publish frontend/ plus the generated config.js. Separate from __init__ so the
        API (whose CORS origin is this distribution) can be created in between."""
        s3_deployment.BucketDeployment(
            self,
            "DeploySite",
            sources=[
                s3_deployment.Source.asset(str(FRONTEND_DIR), exclude=[CONFIG_JS_KEY]),
                s3_deployment.Source.data(CONFIG_JS_KEY, render_config_js(config_values)),
            ],
            destination_bucket=self.bucket,
            distribution=self.distribution,
            distribution_paths=["/*"],
            memory_limit=256,
            log_group=logs.LogGroup(
                self,
                "DeploySiteLogs",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
