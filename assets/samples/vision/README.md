# 图意审校验收样例

两张图均为 InkRAG 本轮制作的合成测试数据，可以放入开发知识库反复使用。以下事实为人工验收参考。

| 图片 | 问题 | 应识别的可见事实 |
| --- | --- | --- |
| [warehouse-flow.png](warehouse-flow.png) | 封条完整和不完整分别走哪条路径？ | Receive → Seal intact?。Yes 指向 Store。No 指向 Inspect，Inspect 再指向 Quarantine。 |
| [ticket-chart.png](ticket-chart.png) | 两个区域的趋势分别是什么？何时持平？ | 4、5、6 月 North 分别为 25、50、75，South 为 50、50、25。5 月持平，6 月 North 高于 South 50。 |

通过支持保留图片的文档格式导入，进入资料详情并选择对应图片。HTML 可以通过以下方式直接生成两份独立的验收文档。页面上传时选择「内置」且保留图片，OCR 可以关闭。

```sh
python3 - <<'PY'
from base64 import b64encode
from pathlib import Path
from tempfile import mkdtemp

destination = Path(mkdtemp(prefix="inkrag-vision-"))
for source in Path("assets/samples/vision").glob("*.png"):
    image = b64encode(source.read_bytes()).decode()
    (destination / f"{source.stem}.html").write_text(
        f'<!doctype html><html><body><h1>{source.stem}</h1>'
        f'<img src="data:image/png;base64,{image}"></body></html>',
        encoding="utf-8",
    )
print(destination)
PY
```

对照原图检查箭头方向、数值与系列名称。模型生成后检查资料版本保持不变。审核并保存后检查新版本，并在发布后用表内问题检索，确认命中审核文字及其对应原图。
