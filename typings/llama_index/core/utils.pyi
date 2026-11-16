"""补齐上游默认tokenizer的整数token列表类型。"""

from collections.abc import Callable

def get_tokenizer(model_name: str = "gpt-3.5-turbo") -> Callable[[str], list[int]]: ...
