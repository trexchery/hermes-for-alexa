import inspect
import io
import json
import zipfile

from deploy import aws_deploy, cloudflare_access, skill_deploy


def test_runtime_policy_is_least_privilege():
    pol = aws_deploy.runtime_policy("us-east-1", "123456789012")
    actions = [a for s in pol["Statement"] for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])]
    assert sorted(actions) == ["logs:CreateLogStream", "logs:PutLogEvents", "ssm:GetParametersByPath"]
    resources = [s["Resource"] for s in pol["Statement"]]
    assert "*" not in json.dumps(resources).replace(":*\"", "").replace("/*\"", "")
    assert resources[0] == "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/hermes-alexa:*"
    assert resources[1] == ["arn:aws:ssm:us-east-1:123456789012:parameter/hermes-alexa",
                            "arn:aws:ssm:us-east-1:123456789012:parameter/hermes-alexa/*"]
    assert all(s["Effect"] == "Allow" for s in pol["Statement"])


def test_trust_policy_only_lambda():
    assert aws_deploy.trust_policy()["Statement"][0]["Principal"] == {"Service": "lambda.amazonaws.com"}


def test_zip_contains_only_the_package(tmp_path):
    pkg = tmp_path / "hermes_alexa_skill"
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"x")
    (pkg / "sub").mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "sub" / "m.py").write_text("a=1")
    names = zipfile.ZipFile(io.BytesIO(aws_deploy.build_zip(pkg))).namelist()
    assert names == ["hermes_alexa_skill/__init__.py", "hermes_alexa_skill/sub/m.py"]
    assert aws_deploy.build_zip(pkg) == aws_deploy.build_zip(pkg)  # deterministic


def test_real_package_zips_without_secrets():
    names = zipfile.ZipFile(io.BytesIO(aws_deploy.build_zip())).namelist()
    assert names and all(n.startswith("hermes_alexa_skill/") for n in names)
    assert not any(n.endswith((".env", ".pem", ".bin")) for n in names)


def test_manifest_placeholders_filled():
    m = {"manifest": {"apis": {"custom": {"endpoint": {"uri": "__LAMBDA_ARN__"}}}, "p": "__PRIVACY_POLICY_URL__"}}
    out = skill_deploy.fill_manifest(m, "arn:aws:lambda:us-east-1:1:function:hermes-alexa", "https://x/p")
    assert "__" not in json.dumps(out)
    assert out["manifest"]["apis"]["custom"]["endpoint"]["uri"].endswith("function:hermes-alexa")


def test_access_policy_is_service_auth_only():
    p = cloudflare_access.policy_payload("tok-1")
    assert p["decision"] == "non_identity" and p["include"] == [{"service_token": {"token_id": "tok-1"}}]
    assert cloudflare_access.app_payload("ha.example.com")["domain"] == "ha.example.com"


def test_smapi_subcommands_exist_in_ask_cli():
    # ask-cli has no "list-vendors"; the real name is get-vendor-list
    assert 'smapi("get-vendor-list")' in inspect.getsource(skill_deploy)
    assert "list-vendors" not in (aws_deploy.SRC.parents[1] / "deploy" / "install.ps1").read_text(encoding="utf-8")


def test_first_create_has_no_endpoint_but_keeps_the_rest():
    from deploy.skill_deploy import without_endpoint
    m = {"apis": {"custom": {"endpoint": {"uri": "arn"}, "interfaces": [1]}}, "publishingInformation": {}}
    bare = without_endpoint(m)
    assert "endpoint" not in bare["apis"]["custom"] and bare["apis"]["custom"]["interfaces"] == [1]
    assert m["apis"]["custom"]["endpoint"] == {"uri": "arn"}  # original untouched


def test_ensure_role_passes_region_to_policy():
    class NoSuch(Exception):
        pass

    class Iam:
        class exceptions:
            NoSuchEntityException = NoSuch

        policy = None

        def get_role(self, RoleName):
            return {"Role": {"Arn": "arn:aws:iam::123456789012:role/r"}}

        def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
            self.policy = PolicyDocument

    iam = Iam()
    assert aws_deploy.ensure_role(iam, "123456789012", "eu-west-1").endswith(":role/r")
    assert "eu-west-1" in iam.policy
