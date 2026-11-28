#!/usr/bin/env python3
"""Generate the architecture diagram from the CDK app with CDK Graph (AWS PDK).

Synthesizes the same stacks as app.py (both the main stack and the optional HealthLake stack)
into a temporary cdk.out, lets CDK Graph's diagram plugin render them, and copies the results
to docs/images/. No AWS calls.

Setup (separate virtualenv, because aws-pdk is unsupported and heavy):
    python3 -m venv .venv-diagram && source .venv-diagram/bin/activate
    pip install -r requirements-diagram.txt
    python scripts/generate_architecture_diagram.py
"""

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import aws_cdk as cdk  # noqa: E402
from aws_pdk.cdk_graph import CdkGraph, FilterPreset, Filters, FilterValue, IFilter, IGraphFilterPlan  # noqa: E402
from aws_pdk.cdk_graph_plugin_diagram import CdkGraphDiagramPlugin, DiagramFormat, IDiagramConfig  # noqa: E402

from voice_to_fhir.build import build_app  # noqa: E402

OUT = ROOT / "docs" / "images"
# Overview: hide log groups, IAM, CloudFront plumbing and custom resources (CDK's site/sample deployment,
# bucket notifications, auto-delete; also the demo-user setup) so the main components stand out. Detailed: everything except CDK internals.
# (Typed jsii structs: nested plain dicts were silently ignored for the filter plan.)
OVERVIEW_FILTERS = [
    IFilter(store=Filters.collapse_cdk_owned_resources()),
    IFilter(store=Filters.prune_custom_resources()),
    IFilter(graph=Filters.exclude_cfn_type([
        FilterValue(value="AWS::Logs::LogGroup"), FilterValue(regex="^Custom::.*"), FilterValue(regex="^AWS::IAM::.*"),
        FilterValue(value="AWS::CloudFront::ResponseHeadersPolicy"),
        FilterValue(value="AWS::CloudFront::OriginAccessControl")])),
    IFilter(store=Filters.prune_empty_containers()),
]
SVG_WIDTH = 2400  # CDK Graph SVGs only have a viewBox; without width/height many viewers render them tiny
DIAGRAMS = [
    IDiagramConfig(name="architecture", title="voice-to-fhir-healthlake-poc: AWS resources (overview)",
                   filter_plan=IGraphFilterPlan(preset=FilterPreset.COMPACT, filters=OVERVIEW_FILTERS)),
    IDiagramConfig(name="architecture-detailed", title="voice-to-fhir-healthlake-poc: AWS resources (detailed)",
                   filter_plan=IGraphFilterPlan(preset=FilterPreset.NON_EXTRANEOUS)),
]


def add_svg_size(path: Path) -> None:
    """Give the root <svg> an explicit width/height matching its viewBox aspect ratio."""
    svg = path.read_text()
    m = re.search(r'<svg\b[^>]*viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    if not m or re.search(r"<svg\b[^>]*\swidth=", svg):
        return
    height = round(SVG_WIDTH * float(m[2]) / float(m[1]))
    path.write_text(svg.replace("<svg ", f'<svg width="{SVG_WIDTH}" height="{height}" ', 1))


def main() -> int:
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    context["existingHealthLakeDatastoreId"] = ""  # include the optional HealthLake stack in the diagram
    with tempfile.TemporaryDirectory() as outdir:
        app = cdk.App(outdir=outdir, context=context)
        build_app(app)
        graph = CdkGraph(app, plugins=[CdkGraphDiagramPlugin(
            defaults={"format": [DiagramFormat.SVG, DiagramFormat.PNG]}, diagrams=DIAGRAMS)])
        app.synth()
        graph.report()
        OUT.mkdir(parents=True, exist_ok=True)
        # The plugin writes cdkgraph/diagram.<name>.<format>; keep the SVG and PNG as docs/images/<name>.<format>.
        # SVGs (icons embedded, small) for both; PNG only for the overview (the detailed PNG is ~3 MB).
        produced = sorted(p for p in Path(outdir, "cdkgraph").glob("diagram.architecture*")
                          if p.suffix == ".svg" or p.name == "diagram.architecture.png")
        for path in produced:
            target = OUT / path.name.removeprefix("diagram.")
            shutil.copy(path, target)
            if target.suffix == ".svg":
                add_svg_size(target)
            print(f"docs/images/{target.name}  ({target.stat().st_size // 1024} KB)")
    return 0 if produced else 1


if __name__ == "__main__":
    sys.exit(main())
