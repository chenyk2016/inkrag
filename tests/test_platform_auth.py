"""用合成签名密钥验证 OIDC 的发现边界和实际 JWT 验签。"""

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from inkrag_platform import auth
from inkrag_platform.models import DomainError
from jwt.algorithms import RSAAlgorithm
from pydantic import JsonValue, TypeAdapter


@pytest.mark.parametrize(
    ("jwks_url", "require_https", "allowed"),
    [
        ("http://key-provider.test/jwks", True, False),
        ("https://key-provider.test/jwks", True, True),
        ("http://key-provider.test/jwks", False, True),
    ],
)
def test_discovered_signing_keys_respect_deployment_tls(
    monkeypatch: pytest.MonkeyPatch, jwks_url: str, require_https: bool, allowed: bool
) -> None:
    """生产发现不能绕过 TLS；开发可用明文端点，可信 HTTPS 发现仍能验签。"""
    issuer = "https://identity.test/realm"
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = TypeAdapter(dict[str, JsonValue]).validate_json(
        RSAAlgorithm.to_jwk(signing_key.public_key())
    )
    jwk["kid"] = "fixture-signing-key"
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": issuer,
            "aud": "inkrag-test",
            "sub": "test-admin",
            "tenant_id": "test-tenant",
            "roles": ["admin"],
            "iat": now,
            "exp": now + 60,
        },
        signing_key,
        algorithm="RS256",
        headers={"kid": "fixture-signing-key"},
    )
    discovered: list[str] = []
    fetched: list[str] = []
    client = httpx.Client

    def discover(request: httpx.Request) -> httpx.Response:
        """发行方响应由合成传输提供，无需连接外部身份服务。"""
        discovered.append(str(request.url))
        return httpx.Response(200, json={"issuer": issuer, "jwks_uri": jwks_url})

    def discovery_client(
        *, timeout: float, trust_env: bool, follow_redirects: bool
    ) -> httpx.Client:
        """保留实际 HTTP 客户端响应语义，只替换网络传输。"""
        return client(
            timeout=timeout,
            trust_env=trust_env,
            follow_redirects=follow_redirects,
            transport=httpx.MockTransport(discover),
        )

    def fetch_keys(self: jwt.PyJWKClient) -> dict[str, JsonValue]:
        """记录实际 JWKS 取钥意图，其余 key 匹配和验签由 PyJWT 执行。"""
        fetched.append(self.uri)
        return {"keys": [jwk]}

    monkeypatch.setattr(auth.httpx, "Client", discovery_client)
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fetch_keys)
    verifier = auth.OIDCVerifier(issuer, "inkrag-test", require_https=require_https)
    if allowed:
        principal = verifier.verify(token)
        assert principal.tenant_id == "test-tenant" and principal.roles == ["admin"]
        assert fetched == [jwks_url]
    else:
        with pytest.raises(DomainError) as denied:
            verifier.verify(token)
        assert denied.value.code == "unauthorized"
        assert fetched == []
    assert discovered == [issuer + "/.well-known/openid-configuration"]
