# InkRAG

InkRAG turns manuals, policies, and other documents into searchable evidence that can be checked against the original. Build a local knowledge base with the Python SDK, or use the self-hosted workspace to review and publish versions for other applications to query.

[中文 README](README.md) · [Documentation](docs/index.md) · [Interactive tutorial (Chinese)](https://chenyk2016.github.io/inkrag/) · [Releases](https://github.com/chenyk2016/inkrag/releases)

## Quickstart

With Python 3.12 and uv installed, run the included Chinese support-manual example. The first run downloads dependencies. The example uses fixed embeddings and needs no API key.

```sh
git clone https://github.com/chenyk2016/inkrag.git
cd inkrag
uv run --locked --no-dev python examples/offline_quickstart.py
```

Look for the answer to error code `E102` and its source, `support.md`. The example exercises ingestion, local search, evidence attribution, and reopening the stored knowledge base. See the [Python SDK](docs/python-sdk.md) for real embeddings and hybrid search, and the [HTTP API](docs/platform-http.md) for search over published versions.

## Components

| Component | Responsibility |
| --- | --- |
| [inkparse](packages/inkparse/README.md) | Parse files and preserve source locations, tables, and related assets |
| [inkstore](packages/inkstore/README.md) | Store originals, derived assets, and immutable records |
| [inkrag](docs/python-sdk.md) | Prepare chunks and retrieve evidence with keyword, vector, or hybrid search |
| [inkrag-platform](docs/platform-implementation.md) | Manage access, import tasks, review, and published versions |
| [Web workspace](web/README.md) | Review documents, try queries, and manage versions |

The SDK runs locally with SQLite. The workspace uses PostgreSQL, OpenSearch, S3-compatible storage, RabbitMQ, an embedding service, and OIDC. See the [development quickstart](README.md#3-运行工作台) and the [production deployment guide](docs/deployment.md). The first public version is an alpha. See [release readiness](docs/release-readiness.md) for verified paths and deployment checks.

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Apache-2.0](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)
