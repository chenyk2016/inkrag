"""解析结果与通用资产服务的适配，业务解释不进入存储包。"""

from inkstore import AssetStorage, ResourceReference, StoredRecord

from .models import ParseError, ParseResult


def save_stored_result(result: ParseResult, storage: AssetStorage) -> None:
    """验证解析语义后映射为通用记录；只使用注入接口，不创建后端。"""
    result = ParseResult.model_validate_json(result.model_dump_json())
    if result.source.asset_id is None:
        raise ParseError("source_asset_required", "结果没有原件资产")
    if storage.get(result.source.asset_id).size_bytes != result.source.size_bytes:
        raise ParseError("invalid_asset", "原件大小与解析结果不一致")
    record = StoredRecord(
        record_id=result.result_id,
        source_asset_id=result.source.asset_id,
        payload=result.model_dump_json(),
        references=[
            ResourceReference(asset_id=a.asset_id, locator=a.element_ref, role=a.role, page=a.page)
            for a in result.assets
        ],
    )
    storage.save_record(record)


def load_stored_result(storage: AssetStorage, result_id: str) -> ParseResult:
    """由业务层还原并校验存储记录和解析引用一致，不要求具体后端类型。"""
    record = storage.load_record(result_id)
    result = ParseResult.model_validate_json(record.payload)
    refs = [
        ResourceReference(asset_id=a.asset_id, locator=a.element_ref, role=a.role, page=a.page)
        for a in result.assets
    ]
    if (
        result.result_id != result_id
        or record.record_id != result_id
        or result.source.asset_id != record.source_asset_id
        or refs != record.references
    ):
        raise ParseError("invalid_record", "存储记录与解析引用不一致")
    if storage.get(record.source_asset_id).size_bytes != result.source.size_bytes:
        raise ParseError("invalid_asset", "原件大小与记录不一致")
    return result


def copy_result(result: ParseResult, source: AssetStorage, target: AssetStorage) -> None:
    """调用方显式迁移引用资源；业务包不自行选择仓库或复制策略。"""
    result = ParseResult.model_validate_json(result.model_dump_json())
    if result.source.asset_id is None:
        raise ParseError("source_asset_required", "结果没有原件资产")
    ids = {result.source.asset_id, *(a.asset_id for a in result.assets)}
    for asset_id in sorted(ids):
        asset = target.put_bytes(source.read(asset_id))
        if asset.asset_id != asset_id:
            raise ParseError("invalid_asset", "存储返回的资产摘要不一致")
    save_stored_result(result, target)
