#!/usr/bin/env python3
"""helmreview.py regression tests: HV004 flags secrets, not references to a Secret.

    python3 skills/helm-chart-review/tests/test_helmreview.py
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import helmreview as hr  # noqa: E402

# A Secret named by reference: the fix HV004 itself recommends ("use existingSecret/external-secrets")
REFERENCE = """database:
  existingSecret: api-db
  existingSecretPasswordKey: password
  passwordSecretName: api-db
"""
SECRET = "database:\n  password: hunter2\n"


def hv004(rep):
    return [f["message"] for f in rep.findings if f["rule"] == "HV004"]


class SecretReferences(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)

    def write(self, name, text):
        p = os.path.join(self.d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def chart(self, values):
        self.write("chart/Chart.yaml", "apiVersion: v2\nname: api\nversion: 0.1.0\n")
        self.write("chart/values.yaml", values)
        rep = hr.Report(hr.DEFAULT_POLICY)
        hr.review_chart(os.path.join(self.d, "chart"), rep, "values*.yaml")
        return rep

    def gitops(self, text):
        rep = hr.Report(hr.DEFAULT_POLICY)
        hr.review_gitops([self.write("gitops/app.yaml", text)], rep, None)
        return rep

    def test_values_file_reference_is_not_a_secret(self):
        self.assertEqual(hv004(self.chart(REFERENCE)), [])
        self.assertEqual(len(hv004(self.chart(SECRET))), 1)

    def test_argo_inline_reference_is_not_a_secret(self):
        app = ("apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata: {{name: api}}\nspec:\n"
               "  source:\n    targetRevision: 1.0.0\n    helm:\n      values: |\n{}")
        indent = lambda s: "".join("        " + ln + "\n" for ln in s.splitlines())  # noqa: E731
        self.assertEqual(hv004(self.gitops(app.format(indent(REFERENCE)))), [])
        self.assertEqual(len(hv004(self.gitops(app.format(indent(SECRET))))), 1)

    def test_flux_inline_reference_is_not_a_secret(self):
        hrel = ("apiVersion: helm.toolkit.fluxcd.io/v2\nkind: HelmRelease\nmetadata: {{name: api}}\nspec:\n"
                "  chart: {{spec: {{chart: api, version: 1.0.0}}}}\n  values:\n{}")
        indent = lambda s: "".join("    " + ln + "\n" for ln in s.splitlines())  # noqa: E731
        self.assertEqual(hv004(self.gitops(hrel.format(indent(REFERENCE)))), [])
        self.assertEqual(len(hv004(self.gitops(hrel.format(indent(SECRET))))), 1)


if __name__ == "__main__":
    unittest.main()
