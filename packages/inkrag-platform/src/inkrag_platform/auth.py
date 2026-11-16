"""OIDC令牌验证；只信任签名、issuer、audience和已配置身份提供者的范围声明。"""

from threading import Lock
from urllib.parse import urlsplit

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import DomainError, Principal


class RealmRoles(BaseModel):
    """读取Keycloak标准realm角色，其余声明不参与授权。"""

    model_config = ConfigDict(strict=True, extra="ignore")
    roles: list[str] = Field(default_factory=list[str])


class Claims(BaseModel):
    """仅收窄业务使用的已验证JWT声明，不接受客户端自报的HTTP租户参数。"""

    model_config = ConfigDict(strict=True, extra="ignore")
    sub: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=128)
    realm_access: RealmRoles = Field(default_factory=RealmRoles)
    roles: list[str] = Field(default_factory=list[str])


class Discovery(BaseModel):
    """只接受可信issuer声明的标准OIDC配置，不使用token自带jku地址。"""

    model_config = ConfigDict(strict=True, extra="ignore")
    issuer: str
    jwks_uri: str


class OIDCVerifier:
    """原生PyJWT/JWKS验证器；外部issuer与内网JWKS地址可以明确分开配置。"""

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        *,
        require_https: bool = False,
    ) -> None:
        """持有有缓存和超时的JWKS客户端，不在构造时下载身份配置。"""
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.jwks_url = jwks_url
        self.require_https = require_https
        self._jwks: jwt.PyJWKClient | None = None
        self._lock = Lock()

    def _check_url(self, url: str) -> None:
        """生产对配置和发现返回的地址都验证 HTTPS，拒绝无主机或嵌入凭证的地址。"""
        try:
            address = urlsplit(url)
            schemes = {"https"} if self.require_https else {"http", "https"}
            if address.scheme in schemes and address.hostname and address.username is None:
                return
        except ValueError:
            pass
        raise DomainError("unauthorized", "身份服务地址配置无效")

    def _client(self) -> jwt.PyJWKClient:
        """首次按需发现并精确核对issuer，显式内网JWKS覆盖保持可用。"""
        with self._lock:
            if self._jwks is None:
                url = self.jwks_url
                if not url:
                    self._check_url(self.issuer)
                    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                        response = client.get(self.issuer + "/.well-known/openid-configuration")
                        response.raise_for_status()
                        discovery = Discovery.model_validate_json(response.content)
                    if discovery.issuer != self.issuer:
                        raise DomainError("unauthorized", "身份发行方配置不一致")
                    url = discovery.jwks_uri
                self._check_url(url)
                self._jwks = jwt.PyJWKClient(url, cache_keys=False, lifespan=300, timeout=5)
            return self._jwks

    def verify(self, token: str) -> Principal:
        """拒绝无效/过期/错受众签名及缺少租户的令牌，不向调用方暴露原文。"""
        if not token or len(token) > 16384:
            raise DomainError("unauthorized", "身份令牌为空或超过预算")
        try:
            key = self._client().get_signing_key_from_jwt(token)
            decoded = jwt.decode(
                token,
                key.key,
                algorithms=["RS256", "ES256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
                leeway=10,
            )
            claims = Claims.model_validate(decoded)
            roles = sorted(
                set(claims.roles + claims.realm_access.roles) & {"admin", "editor", "viewer"}
            )
            if not roles:
                raise DomainError("forbidden", "当前身份没有工作台角色")
            return Principal(tenant_id=claims.tenant_id, subject=claims.sub, roles=roles)
        except (jwt.PyJWTError, ValidationError, OSError, httpx.HTTPError):
            raise DomainError("unauthorized", "身份令牌无效或已过期") from None
