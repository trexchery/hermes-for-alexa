"""Deploy the Lambda, its role, log group and SSM parameters. Idempotent; secrets go through boto3, never argv.

    python -m deploy.aws_deploy        (needs: pip install boto3; AWS credentials configured)
Run it before and after skill_deploy: the skill id (known only after the skill exists) is needed for the
invoke permission and the SKILL_ID env, the Lambda ARN is needed by the skill. install.ps1 does both passes.
"""
from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path

from deploy import secrets, state

FUNCTION = "hermes-alexa"
ROLE = "lambda-hermes-alexa-role"
SSM_PREFIX = "/hermes-alexa/"
HANDLER = "hermes_alexa_skill.handler.lambda_handler"  # per src/hermes_alexa_skill/handler.py
SRC = Path(__file__).resolve().parents[1] / "src" / "hermes_alexa_skill"
STATEMENT_ID = "alexa-skill"


def trust_policy() -> dict:
    return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}


def runtime_policy(region: str, account: str) -> dict:
    """Least privilege: write to its own log group, read its own parameters. Nothing else."""
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{FUNCTION}:*"},
        {"Effect": "Allow", "Action": "ssm:GetParametersByPath",  # by-path calls are authorised on the path ARN itself
         "Resource": [f"arn:aws:ssm:{region}:{account}:parameter{SSM_PREFIX.rstrip('/')}",
                      f"arn:aws:ssm:{region}:{account}:parameter{SSM_PREFIX}*"]},
    ]}


def build_zip(src: Path = SRC) -> bytes:
    """Zip of the hermes_alexa_skill package only (stdlib; boto3 comes with the Lambda runtime)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                info = zipfile.ZipInfo(f"{src.name}/{f.relative_to(src).as_posix()}", (2020, 1, 1, 0, 0, 0))  # stable bytes
                info.external_attr = 0o644 << 16
                z.writestr(info, f.read_bytes(), zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def ensure_role(iam, account: str, region: str) -> str:
    try:
        arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust_policy()),
                              Description="hermes-alexa Lambda runtime")["Role"]["Arn"]
        print("  created role")
    iam.put_role_policy(RoleName=ROLE, PolicyName="runtime", PolicyDocument=json.dumps(runtime_policy(region, account)))
    return arn


def put_params(ssm) -> None:
    ssm.put_parameter(Name=SSM_PREFIX + "ha_url", Value=f"https://{state.get('public_hostname')}", Type="String", Overwrite=True)
    for name, secret in (("ha_token", "ha_alexa_token"), ("cf_access_client_id", "cf_access_client_id"),
                         ("cf_access_client_secret", "cf_access_client_secret")):
        if secrets.exists(secret):
            ssm.put_parameter(Name=SSM_PREFIX + name, Value=secrets.get(secret), Type="SecureString", Overwrite=True)
            print(f"  ssm {name}: set")
        elif name == "ha_token":
            raise SystemExit("ha_alexa_token missing from DPAPI: run python -m deploy.bootstrap_ha first")
        else:
            print(f"  ssm {name}: skipped (no Cloudflare Access token stored)")


def ensure_permission(lam, skill_id: str) -> None:
    try:
        pol = json.loads(lam.get_policy(FunctionName=FUNCTION)["Policy"])
        cur = next((s for s in pol["Statement"] if s["Sid"] == STATEMENT_ID), None)
    except lam.exceptions.ResourceNotFoundException:
        cur = None
    if cur and cur.get("Condition", {}).get("StringEquals", {}).get("lambda:EventSourceToken") == skill_id:
        return
    if cur:
        lam.remove_permission(FunctionName=FUNCTION, StatementId=STATEMENT_ID)
    lam.add_permission(FunctionName=FUNCTION, StatementId=STATEMENT_ID, Action="lambda:InvokeFunction",
                       Principal="alexa-appkit.amazon.com", EventSourceToken=skill_id)
    print("  alexa invoke permission set for this skill id only")


def main() -> int:
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        print("boto3 missing: pip install boto3")
        return 1
    skill_id = state.load().get("skill_id")
    region = state.get("aws_region")
    state.get("public_hostname")  # fail early with a clear message
    sts = boto3.client("sts", region_name=region)
    try:
        account = sts.get_caller_identity()["Account"]
    except Exception:
        print("AWS credentials missing or expired. Run once:  aws configure   (or: aws configure sso)")
        return 1
    iam, ssm, lam, logs = (boto3.client(n, region_name=region) for n in ("iam", "ssm", "lambda", "logs"))
    role_arn = ensure_role(iam, account, region)
    try:
        logs.create_log_group(logGroupName=f"/aws/lambda/{FUNCTION}")
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=f"/aws/lambda/{FUNCTION}", retentionInDays=14)
    put_params(ssm)
    env = {"SSM_PREFIX": SSM_PREFIX, "LOG_LEVEL": "INFO", **({"SKILL_ID": skill_id} if skill_id else {})}
    cfg = dict(Runtime="python3.13", Role=role_arn, Handler=HANDLER, Timeout=10, MemorySize=256, Environment={"Variables": env})
    code = build_zip()
    try:
        lam.get_function(FunctionName=FUNCTION)
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=code, Architectures=["arm64"])
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        arn = lam.update_function_configuration(FunctionName=FUNCTION, **cfg)["FunctionArn"]
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)  # State stays Active during the update
        print("  updated function")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(12):  # a new role needs a few seconds to become assumable
            try:
                arn = lam.create_function(FunctionName=FUNCTION, Code={"ZipFile": code}, Architectures=["arm64"], **cfg)["FunctionArn"]
                break
            except ClientError as e:
                if attempt == 11 or "cannot be assumed" not in str(e):
                    raise
                time.sleep(5)
        print("  created function")
    lam.get_waiter("function_active_v2").wait(FunctionName=FUNCTION)
    if skill_id:
        ensure_permission(lam, skill_id)
    else:
        print("  skill id not known yet: run skill_deploy, then this again (adds the invoke permission)")
    state.update(lambda_arn=arn)
    print(f"Lambda ARN: {arn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
