"""显式CLI的本地Docker初始化；仅管理本项目命名空间，不改变外部服务配置。"""

import hashlib
import json
import os
import platform
import secrets
import socket
import subprocess
import time
from pathlib import Path

import boto3
import httpx
from botocore.exceptions import ClientError
from filelock import FileLock
from pydantic import JsonValue, TypeAdapter

RESOURCE_DIR = Path(__file__).parent / "resources"
MODEL_REVISION = "7999e1d3359715c523056ef9478215996d62a620"
PORTS = ("PG_PORT", "RABBIT_PORT", "S3_PORT", "SEARCH_PORT", "OIDC_PORT", "APP_PORT")
DEPENDENCIES = {
    "INKRAG_DATABASE_URL": "postgres",
    "INKRAG_RABBIT_URL": "rabbitmq",
    "INKRAG_S3_ENDPOINT": "minio",
    "INKRAG_OIDC_ISSUER": "keycloak",
    "INKRAG_OPENSEARCH_URL": "opensearch",
    "INKRAG_EMBEDDING_URL": "embedding-online",
    "INKRAG_BATCH_EMBEDDING_URL": "embedding-batch",
}


def private_write(path: Path, content: str) -> None:
    """以仅当前用户可读的原子文件保存初始化配置，秘密不进入日志。"""
    temporary = path.with_suffix(path.suffix + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)
    temporary.replace(path)


def state_directory() -> Path:
    """默认当前项目目录，部署方可用INKRAG_STATE_DIR明确覆盖。"""
    directory = Path(os.environ.get("INKRAG_STATE_DIR", str(Path.cwd() / ".inkrag")))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory.resolve()


def initialize_state(directory: Path) -> dict[str, str]:
    """幂等复用既有密码和卷，不覆盖外部变量，不重置数据库或身份。"""
    config = directory / "compose.env"
    if config.exists():
        config.chmod(0o600)
        values = dict(
            line.split("=", 1)
            for line in config.read_text().splitlines()
            if line and not line.startswith("#")
        )
        # 旧版本先写配置。上次中断时使用旧命名规则找回可能已存在的卷。
        if not (directory / "state.json").exists():
            _write_state(directory, values, identity=directory.parent)
        return values
    values: dict[str, str] = {}
    sockets: list[socket.socket] = []
    try:
        for key in PORTS:
            listener = socket.socket()
            sockets.append(listener)
            listener.bind(("127.0.0.1", 0))
            values[key] = str(listener.getsockname()[1])
    finally:
        for listener in sockets:
            listener.close()
    for key in (
        "PG_BOOTSTRAP_PASSWORD",
        "PG_APP_PASSWORD",
        "PG_MIGRATION_PASSWORD",
        "KEYCLOAK_DB_PASSWORD",
        "RABBIT_PASSWORD",
        "MINIO_SECRET_KEY",
        "OIDC_BOOTSTRAP_PASSWORD",
        "LOCAL_ADMIN_PASSWORD",
    ):
        values[key] = secrets.token_hex(24)
    values.update(
        {
            "MINIO_ACCESS_KEY": "ik" + secrets.token_hex(10),
            "MODEL_REVISION": MODEL_REVISION,
            "TEI_IMAGE": "ghcr.io/huggingface/text-embeddings-inference:"
            + ("cpu-arm64-1.9" if platform.machine() in {"arm64", "aarch64"} else "cpu-1.9"),
            "INKRAG_RESOURCE_DIR": str(RESOURCE_DIR),
            "INKRAG_SOURCE_ROOT": str(Path.cwd()),
        }
    )
    _write_state(directory, values, identity=directory)
    private_write(config, "".join(f"{key}={value}\n" for key, value in values.items()))
    return values


def _write_state(directory: Path, values: dict[str, str], *, identity: Path) -> None:
    """固定 Compose 作用域；独立状态目录拥有独立端口和持久卷。"""
    project = "inkrag-" + hashlib.sha256(str(identity.resolve()).encode()).hexdigest()[:10]
    private_write(
        directory / "state.json",
        json.dumps({"project": project, "ports": {k: values[k] for k in PORTS}}),
    )


def compose(directory: Path, *arguments: str, capture: bool = False) -> str:
    """只操作稳定项目名；输出不包含环境或docker inspect秘密。"""
    state = TypeAdapter(dict[str, JsonValue]).validate_json((directory / "state.json").read_bytes())
    project = state.get("project")
    if not isinstance(project, str) or not project.startswith("inkrag-"):
        raise RuntimeError("本地项目状态无效")
    command = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--env-file",
        str(directory / "compose.env"),
        "-f",
        str(RESOURCE_DIR / "compose.yaml"),
        *arguments,
    ]
    environment = dict(os.environ)
    environment.setdefault("INKRAG_DOCLING_MODELS_DIR", str(directory / "docling-models"))
    if environment.get("INKRAG_MODE") == "production":
        environment.setdefault("INKRAG_SEARCH_REPLICAS", "1")
    if environment.get("INKRAG_OIDC_ISSUER") and not environment.get("INKRAG_OIDC_JWKS_URL"):
        environment["INKRAG_OIDC_JWKS_URL"] = ""
    if not environment.get("INKRAG_S3_ENDPOINT"):
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            environment.pop(key, None)
    else:
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            environment.setdefault(key, "")
    completed = subprocess.run(
        command,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    if completed.returncode:
        raise RuntimeError("本项目Docker操作失败；配置已保留，可修复后重试")
    return completed.stdout if capture else ""


def wait_http(url: str, *, timeout: int = 180) -> None:
    """有截止时间地等待本项目端点；不给外部失败切换其它后端。"""
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=3, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                if client.get(url).is_success:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
    raise RuntimeError("本地服务启动超时，检查inkrag status和本项目容器日志")


def initialize_identity(values: dict[str, str]) -> None:
    """为本地开发域配置登录主题，首次启动时初始化账号、PKCE 和权限声明。"""
    base = "http://127.0.0.1:" + values["OIDC_PORT"]
    wait_http(base + "/realms/master/.well-known/openid-configuration")
    with httpx.Client(base_url=base, timeout=15, trust_env=False) as client:
        response = client.post(
            "/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": "bootstrap",
                "password": values["OIDC_BOOTSTRAP_PASSWORD"],
            },
        )
        response.raise_for_status()
        token = (
            TypeAdapter(dict[str, JsonValue]).validate_json(response.content).get("access_token")
        )
        if not isinstance(token, str):
            raise RuntimeError("开发身份初始化失败")
        client.headers["Authorization"] = "Bearer " + token
        realm_path = "/admin/realms/inkrag"
        existing = client.get(realm_path)
        theme_settings: dict[str, JsonValue] = {
            "loginTheme": "inkrag",
            "internationalizationEnabled": True,
            "supportedLocales": ["zh-Hans"],
            "defaultLocale": "zh-Hans",
        }
        if existing.status_code == 200:
            realm_config = TypeAdapter(dict[str, JsonValue]).validate_json(existing.content)
            # 只给原来使用默认主题的开发域升级，保留管理员自行选择的主题和语言。
            if realm_config.get("loginTheme") in (None, "", "keycloak", "keycloak.v2"):
                update = (
                    {"loginTheme": "inkrag"}
                    if realm_config.get("internationalizationEnabled")
                    else theme_settings
                )
                client.put(realm_path, json=update).raise_for_status()
            return
        if existing.status_code != 404:
            existing.raise_for_status()
        app_url = "http://127.0.0.1:" + values["APP_PORT"]
        realm: dict[str, JsonValue] = {
            "realm": "inkrag",
            "enabled": True,
            **theme_settings,
            "sslRequired": "none",
            "registrationAllowed": False,
            "accessTokenLifespan": 300,
            "roles": {"realm": [{"name": role} for role in ("admin", "editor", "viewer")]},
            "clients": [
                {
                    "clientId": "inkrag-web",
                    "publicClient": True,
                    "standardFlowEnabled": True,
                    "directAccessGrantsEnabled": True,
                    "redirectUris": [app_url + "/*"],
                    "webOrigins": [app_url],
                    "attributes": {"pkce.code.challenge.method": "S256"},
                    "protocolMappers": [
                        {
                            "name": "tenant",
                            "protocol": "openid-connect",
                            "protocolMapper": "oidc-hardcoded-claim-mapper",
                            "config": {
                                "claim.name": "tenant_id",
                                "claim.value": "local-development",
                                "jsonType.label": "String",
                                "access.token.claim": "true",
                                "id.token.claim": "true",
                            },
                        },
                        {
                            "name": "audience",
                            "protocol": "openid-connect",
                            "protocolMapper": "oidc-audience-mapper",
                            "config": {
                                "included.client.audience": "inkrag-web",
                                "access.token.claim": "true",
                                "id.token.claim": "false",
                            },
                        },
                    ],
                }
            ],
            "users": [
                {
                    "username": "admin",
                    "enabled": True,
                    "emailVerified": True,
                    "firstName": "Local",
                    "lastName": "Administrator",
                    "email": "admin@localhost.invalid",
                    "realmRoles": ["admin"],
                    "credentials": [
                        {
                            "type": "password",
                            "value": values["LOCAL_ADMIN_PASSWORD"],
                            "temporary": False,
                        }
                    ],
                }
            ],
        }
        created = client.post("/admin/realms", json=realm)
        if created.status_code != 201:
            raise RuntimeError("开发realm初始化失败，请检查身份服务日志")


def initialize_bucket(values: dict[str, str]) -> None:
    """仅为本项目自建MinIO创建桶，外部桶/IAM由部署方预先准备。"""
    endpoint = "http://127.0.0.1:" + values["S3_PORT"]
    wait_http(endpoint + "/minio/health/ready")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=values["MINIO_ACCESS_KEY"],
        aws_secret_access_key=values["MINIO_SECRET_KEY"],
    )
    try:
        try:
            client.head_bucket(Bucket="inkrag")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            try:
                client.create_bucket(Bucket="inkrag")
            except ClientError as conflict:
                if conflict.response.get("Error", {}).get("Code") != "BucketAlreadyOwnedByYou":
                    raise
            # 并发初始化或已有桶必须确实可用，不能吞掉权限/他人同名桶冲突。
            client.head_bucket(Bucket="inkrag")
    finally:
        client.close()


def up(*, rebuild: bool = False) -> None:
    """有配置用外部服务，缺省启动真实开发依赖；生产不自动创建开发拓扑。"""
    production = os.environ.get("INKRAG_MODE") == "production"
    if production:
        from .settings import load_settings

        load_settings()
        if not os.environ.get("INKRAG_APP_IMAGE") or not os.environ.get("INKRAG_MIGRATION_URL"):
            raise RuntimeError("生产初始化需已构建的INKRAG_APP_IMAGE和单独的INKRAG_MIGRATION_URL")
    directory = state_directory()
    with FileLock(str(directory / "bootstrap.lock"), timeout=5):
        values = initialize_state(directory)
        missing = [
            service for variable, service in DEPENDENCIES.items() if not os.environ.get(variable)
        ]
        if os.environ.get("INKRAG_CAPABILITIES") == "parse":
            missing = [
                s for s in missing if s not in {"opensearch", "embedding-online", "embedding-batch"}
            ]
        infrastructure = [s for s in missing if not s.startswith("embedding-")]
        if infrastructure:
            print("启动本项目缺省基础服务；外部配置不会被替换。", flush=True)
            compose(directory, "up", "-d", *infrastructure)
        if "keycloak" in missing:
            initialize_identity(values)
            private_write(
                directory / "access.json",
                json.dumps(
                    {
                        "url": "http://127.0.0.1:" + values["APP_PORT"],
                        "username": "admin",
                        "password": values["LOCAL_ADMIN_PASSWORD"],
                    },
                    indent=2,
                ),
            )
        if "minio" in missing:
            initialize_bucket(values)
        source = Path(values["INKRAG_SOURCE_ROOT"])
        image = os.environ.get("INKRAG_APP_IMAGE", "inkrag-platform:0.1.0a1")
        present = subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if production and (rebuild or present.returncode):
            raise RuntimeError("生产镜像必须预先通过供应链门禁并拉取，不允许启动时临时构建")
        if rebuild or present.returncode:
            if not (source / "Dockerfile.platform").is_file():
                raise RuntimeError("源码目录不存在，请配置已构建的INKRAG_APP_IMAGE")
            print("构建锁定依赖的应用镜像。", flush=True)
            command = ["docker", "build", "-f", str(source / "Dockerfile.platform"), "-t", image]
            for key in ("HTTP_PROXY", "HTTPS_PROXY"):
                if values.get("MODEL_HTTP_PROXY"):
                    command += ["--build-arg", key + "=" + values["MODEL_HTTP_PROXY"]]
            command.append(str(source))
            if subprocess.run(command, check=False).returncode:
                raise RuntimeError("应用镜像构建失败；修复后可安全重试")
        if "postgres" not in missing and not os.environ.get("INKRAG_MIGRATION_URL"):
            raise RuntimeError("外部PostgreSQL需提供INKRAG_MIGRATION_URL单独迁移凭据")
        compose(directory, "run", "--rm", "migrate")
        print("启动在线/批量模型及API、任务处理和投递进程。", flush=True)
        compose(
            directory,
            "up",
            "-d",
            *[s for s in missing if s.startswith("embedding-")],
            "api",
            "worker",
            "relay",
        )
        address = "http://127.0.0.1:" + values["APP_PORT"]
        wait_http(address + "/health/ready", timeout=600)
        print("工作台：" + address, flush=True)
        if "keycloak" in missing:
            print("本地初始账号文件：" + str(directory / "access.json"), flush=True)


def manage(command: str) -> None:
    """查看或停止自建项目；down保留所有持久卷，不提供隐式清空。"""
    directory = state_directory()
    if not (directory / "state.json").exists():
        raise RuntimeError("尚未初始化本地服务")
    if command == "down":
        compose(directory, "--profile", "app", "down")
    else:
        print(compose(directory, "--profile", "app", "ps", "--format", "table", capture=True))
