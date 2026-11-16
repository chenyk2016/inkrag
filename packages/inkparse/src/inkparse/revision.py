"""从已验证结果创建不可变文字修订，保留原件、资源与原生定位。"""

from pydantic import TypeAdapter

from .editing import TextOperation, revise_structure
from .models import ParsedPayload, ParseError, ParseResult, fingerprint


def revise_result(result: ParseResult, changes: dict[str, str]) -> ParseResult:
    """按原生文本引用替换文字，生成带前序ID的新版本，不写存储或修改输入。

    不支持插入、删除或移动元素，因此页码、元素引用、原始识别文字及图片关联
    都保持。修订文字不能被解释为新的OCR定位；全空、未知引用及无实际变化拒绝。
    """
    changes = TypeAdapter(dict[str, str]).validate_python(changes, strict=True)
    result = ParseResult.model_validate_json(result.model_dump_json())
    if result.schema_version == "inkparse.result.v4":
        return revise_structure(
            result,
            [
                TextOperation(kind="text", element_ref=reference, text=text)
                for reference, text in changes.items()
            ],
        )
    if not changes or len(changes) > 10000:
        raise ParseError("invalid_revision", "请选择1到10000个文本元素进行修订")
    items = {item.self_ref: item for item in result.document.texts}
    changed = False
    for reference, text in changes.items():
        if reference not in items:
            raise ParseError("invalid_revision", "修订只接受存在的文本引用及文字")
        if len(text.encode()) > 1024 * 1024:
            raise ParseError("invalid_revision", "单个文字元素超过1MiB")
        changed = changed or items[reference].text != text
        items[reference].text = text
    if not changed or not any(item.text.strip() for item in items.values()):
        raise ParseError("invalid_revision", "没有实际修改或修改后正文为空")
    payload = ParsedPayload.model_validate(
        {
            **result.model_dump(exclude={"result_id"}),
            "document": result.document,
            "revision": result.revision + 1,
            "previous_result_id": result.result_id,
        }
    )
    revised = ParseResult.model_validate(
        {**payload.model_dump(), "result_id": fingerprint(payload)}
    )
    if len(revised.model_dump_json().encode()) > 32 * 1024 * 1024:
        raise ParseError("invalid_revision", "修订后的结果超过32MiB")
    return revised
