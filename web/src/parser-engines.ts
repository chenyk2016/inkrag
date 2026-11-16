/** 将服务端格式声明映射为文件选择范围，选择结果仍由后端再次校验。 */
import type { ParseEngine } from "./api";

const aliases: Record<string, string[]> = {
  text: ["txt"],
  markdown: ["md", "markdown"],
  html: ["html", "htm"],
  image: ["png", "jpg", "jpeg", "webp", "tif", "tiff", "bmp"],
};

/** 独立图片没有原生文字层，默认对整张图识字。 */
export function isImageFile(file: File): boolean {
  return aliases.image.some((extension) =>
    file.name.toLowerCase().endsWith(`.${extension}`),
  );
}

/** 支持语义格式名与扩展名声明，统一处理大小写和前导点。 */
export function engineExtensions(engine: ParseEngine): string[] {
  return [
    ...new Set(
      engine.formats.flatMap((format) => {
        const name = format.toLowerCase().replace(/^\./, "");
        return aliases[name] ?? [name];
      }),
    ),
  ];
}

/** 按单个文件匹配已就绪的解析工具。 */
export function engineSupports(engine: ParseEngine, file: File): boolean {
  const name = file.name.toLowerCase();
  const readyEngine = {
    ...engine,
    formats: engine.formats.filter(
      (format) => !engine.pending_formats.includes(format),
    ),
  };
  return (
    engine.available &&
    engineExtensions(readyEngine).some((extension) =>
      name.endsWith(`.${extension}`),
    )
  );
}

/** 文件选择器只展示已启用的格式。 */
export function uploadAccept(engines: ParseEngine[]): string {
  return [
    ...new Set(
      engines
        .filter((engine) => engine.available)
        .flatMap((engine) =>
          engineExtensions({
            ...engine,
            formats: engine.formats.filter(
              (format) => !engine.pending_formats.includes(format),
            ),
          }),
        ),
    ),
  ]
    .map((extension) => `.${extension}`)
    .join(",");
}

/** OCR适用性以服务声明的格式和当前工具就绪状态共同决定。 */
export function engineSupportsOcr(
  engine: ParseEngine | undefined,
  file: File,
): boolean {
  return Boolean(
    engine &&
      engineSupports(engine, file) &&
      engineExtensions({ ...engine, formats: engine.ocr_formats }).some(
        (extension) => file.name.toLowerCase().endsWith(`.${extension}`),
      ),
  );
}

/** 逐文件校验并冻结工具、OCR及清洗选择；不适用的OCR参数不发送。 */
export function fileParseRules(
  file: File,
  engine: ParseEngine | undefined,
  ocrEnabled: boolean,
  cleaning: { exclude_headers: boolean; exclude_images: boolean },
) {
  if (!engine || !engineSupports(engine, file))
    throw new Error(`${file.name}：请选择可用的解析工具。`);
  return {
    engine: engine.id,
    ...cleaning,
    ...(engineSupportsOcr(engine, file) ? { ocr_enabled: ocrEnabled } : {}),
  };
}
