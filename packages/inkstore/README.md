# inkstore

独立资产存储服务边界及本地实现，不依赖inkparse、Docling、inkrag或LlamaIndex。

```python
from inkstore.local import LocalAssetStore

storage = LocalAssetStore.open("./asset-data")
asset = storage.put_bytes(b"hello")
assert storage.read(asset.asset_id) == b"hello"
```

业务端只使用`inkstore.AssetStorage`协议：put_bytes/get/read/save_record/load_record。具体实现由应用层注入，可换成本地、对象存储或远程客户端。当前提供本地实现和可选S3/MinIO适配器；`inkstore`本身不启动HTTP服务。

记录为StoredRecord：不透明JSON负载、资源引用及不可变身份。存储不解释Docling元素或解析结构。调用者的业务适配层负责定义其语义。

本地实现支持去重、完整性检查、SQLite记录事务、关联删除保护、列举和未引用清理。仓库格式独立使用inkstore.json，拒绝自动打开旧inkparse内置仓库；没有自动迁移、自动清理或结果删除。部署为网络服务时仍需实现认证、鉴权与受控下载。

S3/MinIO使用`from inkstore.s3 import S3AssetStore`，安装可选依赖`s3`；客户端、桶和前缀由调用方提供，具体用法及条件写要求见[对象存储适配](../../docs/s3-storage.md)。基础包导入不会加载boto3。
