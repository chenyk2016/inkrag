# S3与MinIO资产存储

> 本页描述当前接口。部署要求见[生产部署](deployment.md)，当前验收见[发行审查](release-readiness.md)。

`inkstore.s3.S3AssetStore`实现已有的五项`AssetStorage`能力，可直接注入Parser和RAG。业务包不需要区分本地目录、AWS S3或MinIO；接入入口负责选择实现、配置连接并释放客户端。

安装可选依赖：

```sh
pip install './packages/inkstore[s3]'
```

调用方先准备私有普通桶，SDK不会创建桶、修改桶策略、读取解析结构或管理用户身份：

```python
from boto3.session import Session
from botocore.config import Config
from inkstore.s3 import S3AssetStore

# 凭据由宿主通过SDK标准凭据链或显式参数提供，不写入解析结果。
# AWS S3可以省略endpoint_url；MinIO指定自己的HTTPS地址。
client = Session().client(
    "s3",
    endpoint_url="https://minio.example.com",
    region_name="us-east-1",
    config=Config(
        connect_timeout=3,
        read_timeout=30,
        retries={"mode": "standard", "total_max_attempts": 3},
        s3={"addressing_style": "path"},
    ),
)
storage = S3AssetStore(client, bucket="company-documents", prefix="inkstore/v1")
try:
    asset = storage.put_bytes(b"original bytes")
    assert storage.read(asset.asset_id) == b"original bytes"
    # Parser().parse(request, assets=storage)
    # RAG.open(knowledge_path, embedding=embedding, assets=storage)
finally:
    client.close()  # 借入客户端的生命周期始终属于调用方。
```

对象键为`<prefix>/objects/<sha256>`和`<prefix>/records/<record_id>`。资产ID与物理端点、桶或前缀无关，换后端时业务JSON仍使用同一身份。前缀仅接受安全目录组成，不把用户文件路径拼接为对象键。S3客户端负责签名、TLS和连接池；服务端访问策略仍须由部署方提供。

新增对象统一使用`IfNoneMatch="*"`，已有对象不会被覆盖。412表示已存在，适配器读取并验证同一资产或同一记录；409做有限重试，其他连接或权限错误明确失败。AWS的[条件写说明](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html)与MinIO的[兼容表](https://docs.min.io/aistor/developers/s3-api-compatibility/)提供此操作依据。不能把任意声明“S3兼容”的端点当作已经验证；端点必须实际支持条件写和读后写一致性，不能关闭条件头换取成功。

每次读取资产按真实字节校验SHA256，ETag不作为内容身份；写入的Content-MD5只用于传输校验。当前单资产上限20MiB、通用记录JSON负载上限32MiB，S3序列化记录另限64MiB。`get`也会读取完整字节以满足完整性约定，因此大资产、远距离网络和大量重复读取的成本需要按业务实测，未宣称吞吐指标。

结果记录先验证原件和附件存在，再以单个条件PutObject提交。该对象是记录的发布点；多份资源与记录不构成跨对象事务，失败可留下未引用对象，安全重试按相同ID核对。不提供S3删除或自动GC，避免在缺乏统一引用事务时误删仍被其他记录使用的资产。生产角色通常只需目标前缀的`GetObject/PutObject`；不要给应用外部覆盖或删除已发布对象的权限，也不要对活跃前缀设置不区分引用的过期规则。备份、版本化、加密、审计和灾难恢复由对象存储运维负责；本适配器不代替这些配置。

当前验证：五项SDK边界检查及真实MinIO `RELEASE.2025-09-07T16-13-09Z`网络流程通过，包含并发资产去重、并发幂等记录、不可变冲突、重开和损坏拒绝，见[验收记录](release-readiness.md)。尚未使用真实AWS账户、跨地域网络或生产容量进行验收。单元桩不作为云兼容证据。

真实集成检查需显式设置`INKSTORE_S3_INTEGRATION=1`和专用`INKSTORE_TEST_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET`，运行`pytest tests/test_s3_integration.py -s`。只向随机前缀写入并清理该前缀；测试账号必须为隔离验收环境提供所需的列举和删除权限，不能用生产资料前缀。
