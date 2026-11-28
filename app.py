#!/usr/bin/env python3
import aws_cdk as cdk

from voice_to_fhir.build import build_app

app = cdk.App()
build_app(app)
app.synth()
