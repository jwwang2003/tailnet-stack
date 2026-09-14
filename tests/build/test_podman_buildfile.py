import importlib.util
from pathlib import Path
import unittest
p=Path(__file__).resolve().parents[2]/'scripts/prepare-podman-buildfile.py'
spec=importlib.util.spec_from_file_location('podman_buildfile',p)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class PodmanBuildfileTests(unittest.TestCase):
    def test_stages_variables_and_qualified_registries_preserved(self):
        source='ARG GO_BUILDER=golang:1.26.5\nFROM ${GO_BUILDER} AS builder\nFROM --platform=$BUILDPLATFORM node:24-slim AS frontend\nFROM builder AS other\nFROM gcr.io/distroless/static-debian12:nonroot\nCOPY --from=builder /app /app\n'
        result=m.prepare(source)
        self.assertIn('ARG GO_BUILDER=docker.io/library/golang:1.26.5',result)
        self.assertIn('FROM ${GO_BUILDER} AS builder',result)
        self.assertIn('FROM --platform=$BUILDPLATFORM docker.io/library/node:24-slim',result)
        self.assertIn('FROM builder AS other',result)
        self.assertIn('FROM gcr.io/distroless/',result)
        self.assertIn('COPY --from=builder',result)

    def test_namespace_private_registry_and_scratch(self):
        for original,expected in [('alpine:3.22','docker.io/library/alpine:3.22'),('org/image:tag','docker.io/org/image:tag'),('registry.local:5000/image:tag','registry.local:5000/image:tag'),('scratch','scratch')]:
            self.assertEqual(m.qualify(original),expected)
