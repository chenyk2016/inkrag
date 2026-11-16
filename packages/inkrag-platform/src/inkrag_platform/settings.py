"""启动配置只在装配入口读取，生产缺失依赖明确拒绝，不静默创建本地服务。"""

import os
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, model_validator

from .models import MAX_BUILD_DOCUMENTS, MAX_MANIFEST_BYTES

if TYPE_CHECKING:
    from inkrag.lexical import LexicalProfile


class Settings(BaseModel):
    """完整基础设施引用，秘密字段不进入repr或公共能力响应。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    mode: Literal["development", "production"] = "development"
    capabilities: Literal["parse", "rag", "combined"] = "combined"
    database_url: SecretStr
    migration_url: SecretStr | None = None
    rabbit_url: SecretStr
    s3_endpoint: str
    s3_bucket: str = "inkrag"
    s3_prefix: str = "platform/v1"
    opensearch_url: str | None = None
    opensearch_user: SecretStr | None = None
    opensearch_password: SecretStr | None = None
    embedding_url: str | None = None
    batch_embedding_url: str | None = None
    model_revision: str | None = None
    local_model_base_url: str | None = None
    local_model_id: str | None = None
    local_model_disable_reasoning: bool = False
    vision_provider: Literal["siliconflow", "local", "disabled"] = "siliconflow"
    vision_model_id: str | None = Field(default=None, min_length=1, max_length=200)
    siliconflow_api_key: SecretStr | None = None
    oidc_issuer: str
    oidc_jwks_url: str | None = None
    oidc_audience: str = "inkrag-web"
    oidc_client_id: str = "inkrag-web"
    search_replicas: int = Field(default=1, ge=0, le=5)
    lexical_profile: str = Field(default="legacy", min_length=1)
    sandbox_required: bool = True
    parse_models_path: str | None = None
    docling_memory_mb: int = Field(default=4096, ge=512, le=16384)
    docling_seconds: int = Field(default=180, ge=5, le=1800)
    parse_memory_mb: int = Field(default=512, ge=128, le=8192)
    parse_seconds: int = Field(default=60, ge=5, le=1800)
    worker_concurrency: int = Field(default=1, ge=1, le=32)
    job_seconds: int = Field(default=3600, ge=60, le=86400)
    max_build_documents: int = Field(default=MAX_BUILD_DOCUMENTS, ge=1, le=MAX_BUILD_DOCUMENTS)
    max_manifest_bytes: int = Field(default=MAX_MANIFEST_BYTES, ge=1024, le=MAX_MANIFEST_BYTES)
    max_build_input_bytes: int = Field(default=256 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    bind: str = "127.0.0.1"
    port: int = Field(default=8010, ge=1, le=65535)

    @model_validator(mode="after")
    def check_infrastructure(self) -> "Settings":
        """校验真实后端和生产安全前提，解析独立模式不要求嵌入或搜索。"""
        if not self.database_url.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("平台事实源必须为PostgreSQL/psycopg")
        if not self.rabbit_url.get_secret_value().startswith(("amqp://", "amqps://")):
            raise ValueError("必须配置RabbitMQ")
        if bool(self.opensearch_user) != bool(self.opensearch_password):
            raise ValueError("检索身份和密码必须一起提供")
        if bool(self.local_model_base_url) != bool(self.local_model_id):
            raise ValueError("本机模型地址和标识必须一起提供")
        if self.vision_provider == "local" and not self.local_model_base_url:
            raise ValueError("本机图片理解需要配置本机模型地址和标识")
        if (
            self.vision_model_id is not None
            and self.vision_model_id != self.vision_model_id.strip()
        ):
            raise ValueError("图片理解模型标识不能含首尾空白")
        if self.capabilities != "parse":
            if not all(
                [
                    self.opensearch_url,
                    self.embedding_url,
                    self.batch_embedding_url,
                    self.model_revision,
                ]
            ):
                raise ValueError("RAG需要搜索、在线/批量嵌入及固定模型revision")
            self.search_lexical_profile()
        if self.mode == "production":
            urls = [self.s3_endpoint, self.oidc_issuer]
            if self.oidc_jwks_url:
                urls.append(self.oidc_jwks_url)
            if self.capabilities != "parse":
                urls += [
                    u
                    for u in [self.opensearch_url, self.embedding_url, self.batch_embedding_url]
                    if u
                ]
            if any(not u.startswith("https://") for u in urls):
                raise ValueError("生产服务引用需HTTPS/TLS")
            pg_urls = [self.database_url]
            if self.migration_url:
                pg_urls.append(self.migration_url)
            for url in pg_urls:
                dsn = url.get_secret_value()
                sslmode = parse_qs(urlsplit(dsn).query).get("sslmode")
                if not dsn.startswith("postgresql+psycopg://") or sslmode != ["verify-full"]:
                    raise ValueError("生产PostgreSQL及迁移需sslmode=verify-full验证证书与主机名")
            if not self.rabbit_url.get_secret_value().startswith("amqps://"):
                raise ValueError("生产消息连接需TLS")
            if not self.sandbox_required or (
                self.capabilities != "parse" and self.search_replicas < 1
            ):
                raise ValueError("生产必须启用解析隔离与检索副本")
        return self

    def search_lexical_profile(self) -> "LexicalProfile":
        """按检索核心的版本表校验配置，独立解析进程无需加载该包。"""
        from inkrag.lexical import LexicalProfile

        return TypeAdapter(LexicalProfile).validate_python(self.lexical_profile, strict=True)


def load_settings() -> Settings:
    """只读取当前进程环境，初始化Docker/读取本地文件由显式CLI负责。"""
    values: dict[str, object] = {}
    for name in Settings.model_fields:
        raw = os.environ.get("INKRAG_" + name.upper())
        if raw is None or raw == "":
            continue
        if name in {
            "database_url",
            "migration_url",
            "rabbit_url",
            "opensearch_user",
            "opensearch_password",
            "siliconflow_api_key",
        }:
            values[name] = SecretStr(raw)
        elif name in {
            "search_replicas",
            "parse_memory_mb",
            "docling_memory_mb",
            "docling_seconds",
            "parse_seconds",
            "port",
            "worker_concurrency",
            "job_seconds",
            "max_build_documents",
            "max_manifest_bytes",
            "max_build_input_bytes",
        }:
            values[name] = int(raw)
        elif name in {"sandbox_required", "local_model_disable_reasoning"}:
            if raw.lower() not in {"true", "false"}:
                raise ValueError("布尔配置必须为true或false")
            values[name] = raw.lower() == "true"
        else:
            values[name] = raw
    return Settings.model_validate(values)
