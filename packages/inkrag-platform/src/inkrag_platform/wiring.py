"""唯一基础设施装配入口；核心包只接收借入依赖，实例关闭不改变共享资源。"""

import hashlib
from collections.abc import Generator
from contextlib import ExitStack, contextmanager

import boto3
import httpx
from botocore.config import Config
from inkstore import AssetStorage
from inkstore.s3 import S3AssetStore
from pydantic import JsonValue

from .auth import OIDCVerifier
from .local_model import LocalChatModel
from .models import DomainError, Principal
from .operations import Runtime, SearchPort
from .repository import Repository
from .settings import Settings
from .siliconflow_vision import DEFAULT_VISION_MODEL_ID, SiliconFlowVision


class Authentication:
    """业务凭证实时查撤销记录；用户令牌只交给可信OIDC发行方校验。"""

    def __init__(self, repo: Repository, oidc: OIDCVerifier) -> None:
        """借入有界仓库及JWKS校验器，不在构造时读取外部身份。"""
        self.repo, self.oidc = repo, oidc

    def verify(self, token: str) -> Principal:
        """拒绝无效业务凭证，不将其转发到身份服务。"""
        if token.startswith("ik_"):
            principal = self.repo.authenticate_api_key(token)
            if principal is None:
                raise DomainError("unauthorized", "调用凭证无效或已撤销")
            return principal
        return self.oidc.verify(token)


@contextmanager
def open_runtime(settings: Settings, *, batch: bool = False) -> Generator[Runtime, None, None]:
    """按能力装配真实连接；解析独立部署不导入RAG/模型/搜索SDK。"""
    with ExitStack() as stack:
        repo = Repository(settings.database_url.get_secret_value())
        stack.callback(repo.close)
        repo.check()
        s3 = boto3.Session().client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            config=Config(
                connect_timeout=3,
                read_timeout=30,
                max_pool_connections=20,
                retries={"mode": "standard", "max_attempts": 3},
            ),
        )
        stack.callback(s3.close)

        def assets_for(principal: Principal) -> AssetStorage:
            """租户命名空间禁止跨租户按摘要猜测资产或去重复用。"""
            namespace = hashlib.sha256(principal.tenant_id.encode()).hexdigest()
            return S3AssetStore(
                s3, bucket=settings.s3_bucket, prefix=f"{settings.s3_prefix}/{namespace}"
            )

        search: SearchPort | None = None
        model_identity: dict[str, JsonValue] | None = None
        if settings.capabilities != "parse":
            from opensearchpy import OpenSearch

            from inkrag.search import SearchBackend

            from .embedding import RemoteEmbedding

            url = settings.batch_embedding_url if batch else settings.embedding_url
            if url is None or settings.model_revision is None or settings.opensearch_url is None:
                raise ValueError("缺少检索依赖")
            lexical_profile = settings.search_lexical_profile()
            embedding = RemoteEmbedding(url, settings.model_revision)
            stack.callback(embedding.close)
            model_identity = embedding.metadata
            auth = None
            if settings.opensearch_user and settings.opensearch_password:
                auth = (
                    settings.opensearch_user.get_secret_value(),
                    settings.opensearch_password.get_secret_value(),
                )
            client = OpenSearch(
                [settings.opensearch_url],
                http_auth=auth,
                verify_certs=True,
                timeout=60,
                max_retries=2,
                retry_on_timeout=False,
                pool_maxsize=20,
            )
            stack.callback(client.close)
            search = SearchBackend(
                client,
                embedding,
                model_revision=embedding.model_name,
                dimensions=embedding.dimensions,
                replicas=settings.search_replicas,
                tokenizer=embedding.tokenize,
                max_tokens=embedding.max_text_tokens,
                lexical_profile=lexical_profile,
            )

        health = httpx.Client(timeout=3, trust_env=False)
        stack.callback(health.close)

        def check_ready() -> None:
            """真实探测服务依赖，连接丢失时从负载均衡摘除，不返回假健康。"""
            repo.check()
            s3.head_bucket(Bucket=settings.s3_bucket)
            if settings.capabilities != "parse":
                endpoint = settings.batch_embedding_url if batch else settings.embedding_url
                if endpoint:
                    health.get(endpoint.rstrip("/") + "/health").raise_for_status()
                if settings.opensearch_url:
                    credentials = None
                    if settings.opensearch_user and settings.opensearch_password:
                        credentials = (
                            settings.opensearch_user.get_secret_value(),
                            settings.opensearch_password.get_secret_value(),
                        )
                    health.get(
                        settings.opensearch_url.rstrip("/") + "/_cluster/health", auth=credentials
                    ).raise_for_status()

        local_model = (
            LocalChatModel(
                settings.local_model_base_url,
                settings.local_model_id,
                disable_reasoning=settings.local_model_disable_reasoning,
            )
            if not batch and settings.local_model_base_url and settings.local_model_id
            else None
        )
        vision_model: LocalChatModel | SiliconFlowVision | None = None
        if not batch:
            if settings.vision_provider == "siliconflow" and settings.siliconflow_api_key:
                vision_model = SiliconFlowVision(
                    settings.vision_model_id or DEFAULT_VISION_MODEL_ID,
                    settings.siliconflow_api_key,
                )
            elif settings.vision_provider == "local" and local_model is not None:
                vision_model = (
                    LocalChatModel(
                        local_model.base_url,
                        settings.vision_model_id,
                        disable_reasoning=local_model.disable_reasoning,
                    )
                    if settings.vision_model_id
                    else local_model
                )
        yield Runtime(
            parse_models_path=settings.parse_models_path,
            repo=repo,
            assets_for=assets_for,
            auth=Authentication(
                repo,
                OIDCVerifier(
                    settings.oidc_issuer,
                    settings.oidc_audience,
                    settings.oidc_jwks_url,
                    require_https=settings.mode == "production",
                ),
            ),
            search=search,
            oidc={"issuer": settings.oidc_issuer, "client_id": settings.oidc_client_id},
            mode=settings.capabilities,
            check_ready=check_ready,
            model_identity=model_identity,
            local_model=local_model,
            vision_model=vision_model,
            vision_provider=settings.vision_provider,
        )
