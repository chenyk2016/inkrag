/** 导入参数以解析、处理、清洗分组；所有可编辑值都参与冻结预览。 */
import { useState } from "react";
import { Checkbox, Collapse, InputNumber, Radio, Select, Tooltip } from "antd";
import { CircleHelp } from "lucide-react";
import type {
  ParseEngine,
  ProcessingCapabilities,
  ProcessingOptions,
} from "./api";
import { engineSupports, engineSupportsOcr } from "./parser-engines";

export interface FileParsing {
  engine: string;
  ocr_enabled: boolean;
  ocr_mode: "auto" | "full_page";
  table_structure: boolean;
  table_mode: "fast" | "accurate";
}
/** 把帮助放在对应参数旁边，键盘焦点也可打开说明。 */
function Help({ text }: { text: string }) {
  return (
    <Tooltip title={text}>
      <button type="button" className="parameter-help" aria-label={text}>
        <CircleHelp size={15} />
      </button>
    </Tooltip>
  );
}
/** 按声明格式限定Docling版面参数，Office文件不发送PDF专用参数。 */
export function supportsLayout(
  engine: ParseEngine | undefined,
  file: File,
): boolean {
  return engine?.id === "docling" && engineSupportsOcr(engine, file);
}
/** 展示批次参数与单文件覆盖，默认参数严格来自服务能力。 */
export function UploadParameters({
  files,
  engines,
  parsing,
  onParsing,
  processing,
  onProcessing,
  capabilities,
  custom,
  onCustom,
  excludeHeaders,
  excludeImages,
  onCleaning,
  disabled,
}: {
  files: File[];
  engines: ParseEngine[];
  parsing: FileParsing[];
  onParsing: (indices: number[], patch: Partial<FileParsing>) => void;
  processing: ProcessingOptions | null;
  onProcessing: (value: ProcessingOptions) => void;
  capabilities: ProcessingCapabilities | null;
  custom: boolean;
  onCustom: (value: boolean) => void;
  excludeHeaders: boolean;
  excludeImages: boolean;
  onCleaning: (headers: boolean, images: boolean) => void;
  disabled: boolean;
}) {
  const [scope, setScope] = useState(-1);
  const indices = files.flatMap((_, index) =>
    scope < 0 || scope === index ? [index] : [],
  );
  const selected = indices.map((index) => parsing[index]);
  const engine = selected.every((item) => item.engine === selected[0]?.engine)
    ? selected[0]?.engine
    : undefined;
  const layout = indices.filter((index) =>
    supportsLayout(
      engines.find((item) => item.id === parsing[index].engine),
      files[index],
    ),
  );
  const ocr = layout.filter((index) => parsing[index].ocr_enabled);
  const tables = layout.filter((index) => parsing[index].table_structure);
  const ready = Boolean(
    capabilities?.available && processing && capabilities.max_tokens,
  );
  return (
    <div className="upload-parameter-page">
      <Collapse
        ghost
        defaultActiveKey={["parse", "processing", "clean"]}
        items={[
          {
            key: "parse",
            label: <h2>文件解析设置</h2>,
            children: (
              <>
                <div className="parameter-scope">
                  <span>设置范围</span>
                  <Select
                    aria-label="解析参数应用范围"
                    value={scope}
                    disabled={disabled}
                    onChange={setScope}
                    options={[
                      { value: -1, label: `本批全部 ${files.length} 份资料` },
                      ...files.map((file, index) => ({
                        value: index,
                        label: file.name,
                      })),
                    ]}
                  />
                </div>
                <p className="parameter-note">
                  批量设置覆盖全部适用文件；也可选中单份资料单独调整。混合格式可分别选择工具。
                </p>
                <Radio.Group
                  className="parameter-cards"
                  aria-label="解析工具"
                  value={engine}
                  disabled={disabled}
                  onChange={(event) => {
                    const tool = engines.find(
                      (item) => item.id === event.target.value,
                    );
                    if (tool)
                      onParsing(indices, {
                        engine: tool.id,
                        ocr_enabled: tool.ocr_default,
                      });
                  }}
                >
                  {engines.map((tool) => (
                    <Radio
                      value={tool.id}
                      key={tool.id}
                      disabled={
                        !indices.every((index) =>
                          engineSupports(tool, files[index]),
                        )
                      }
                      className="parameter-card"
                    >
                      <strong>
                        {tool.id === "docling"
                          ? "增强解析 · Docling"
                          : tool.name}
                      </strong>
                      <span>
                        {tool.id === "docling"
                          ? "识别复杂版面、表格及扫描文字，耗时通常更长。"
                          : "提取基础文字和原生结构，适合普通文档。"}
                      </span>
                    </Radio>
                  ))}
                </Radio.Group>
                <div className="parameter-options">
                  <div>
                    <Checkbox
                      disabled={disabled || !layout.length}
                      checked={!!layout.length && ocr.length === layout.length}
                      indeterminate={!!ocr.length && ocr.length < layout.length}
                      onChange={(event) =>
                        onParsing(layout, { ocr_enabled: event.target.checked })
                      }
                    >
                      OCR 文字识别
                    </Checkbox>
                    <Help text="Docling 的 PDF 和图片可用。扫描件开启后才有文字；关闭仍保留已有文本，不进行图片语义理解。" />
                    <small>
                      {layout.length
                        ? `适用于所选 ${layout.length} 份 PDF / 图片`
                        : "仅 Docling 的 PDF / 图片支持"}
                    </small>
                  </div>
                  {!!ocr.length && (
                    <div className="parameter-subrow">
                      <label>识别范围</label>
                      <Radio.Group
                        aria-label="OCR识别范围"
                        disabled={disabled}
                        value={
                          ocr.every(
                            (index) =>
                              parsing[index].ocr_mode ===
                              parsing[ocr[0]].ocr_mode,
                          )
                            ? parsing[ocr[0]].ocr_mode
                            : undefined
                        }
                        onChange={(event) =>
                          onParsing(ocr, { ocr_mode: event.target.value })
                        }
                      >
                        <Radio value="auto">自动识别</Radio>
                        <Radio value="full_page">整页识别</Radio>
                      </Radio.Group>
                      <small>
                        独立图片默认整页识别，PDF 默认自动识别需要 OCR
                        的区域。整页识别适合漏字或文字层异常的扫描件，处理更慢。
                      </small>
                    </div>
                  )}
                  <div>
                    <Checkbox
                      disabled={disabled || !layout.length}
                      checked={
                        !!layout.length && tables.length === layout.length
                      }
                      indeterminate={
                        !!tables.length && tables.length < layout.length
                      }
                      onChange={(event) =>
                        onParsing(layout, {
                          table_structure: event.target.checked,
                        })
                      }
                    >
                      识别表格结构
                    </Checkbox>
                    <Help text="默认开启，为 PDF/图片中的表格恢复行列关系。关闭可减少处理，但复杂表格的上下文可能丢失。" />
                  </div>
                  {!!tables.length && (
                    <div className="parameter-subrow">
                      <label>表格模式</label>
                      <Radio.Group
                        aria-label="表格识别模式"
                        disabled={disabled}
                        value={
                          tables.every(
                            (index) =>
                              parsing[index].table_mode ===
                              parsing[tables[0]].table_mode,
                          )
                            ? parsing[tables[0]].table_mode
                            : undefined
                        }
                        onChange={(event) =>
                          onParsing(tables, { table_mode: event.target.value })
                        }
                      >
                        <Radio value="accurate">精确（默认）</Radio>
                        <Radio value="fast">快速</Radio>
                      </Radio.Group>
                      <small>
                        精确模式优先恢复复杂表格；快速模式减少等待，但可能损失结构细节。两者均不保证识别完全准确。
                      </small>
                    </div>
                  )}
                </div>
                <details className="parameter-file-summary">
                  <summary>查看每份资料的实际解析设置</summary>
                  {files.map((file, index) => (
                    <p key={index}>
                      <strong>{file.name}</strong> ·{" "}
                      {
                        engines.find(
                          (tool) => tool.id === parsing[index].engine,
                        )?.name
                      }{" "}
                      ·{" "}
                      {supportsLayout(
                        engines.find(
                          (tool) => tool.id === parsing[index].engine,
                        ),
                        file,
                      )
                        ? `OCR ${parsing[index].ocr_enabled ? "开启" : "关闭"} / 表格${parsing[index].table_structure ? (parsing[index].table_mode === "fast" ? "快速" : "精确") : "关闭"}`
                        : "使用格式原生解析"}
                    </p>
                  ))}
                </details>
              </>
            ),
          },
          {
            key: "processing",
            label: <h2>数据处理方式设置</h2>,
            children: (
              <>
                <label className="parameter-label">处理方式</label>
                <Radio.Group value="chunks" className="parameter-cards">
                  <Radio
                    value="chunks"
                    className="parameter-card"
                    disabled={!ready}
                  >
                    <strong>分块存储</strong>
                    <span>把正文拆成带来源的片段，用于检索和引用。</span>
                  </Radio>
                  <Radio value="qa" disabled className="parameter-card">
                    <strong>问答对提取</strong>
                    <span>
                      {capabilities?.qa_pairs.reason ||
                        "尚未接入生成模型与问答提取流程。"}
                    </span>
                  </Radio>
                </Radio.Group>
                {!ready && (
                  <p className="parameter-note">
                    {capabilities?.reason ||
                      "当前服务未开放分块配置，仅保存解析结果。"}
                  </p>
                )}
                {ready && processing && (
                  <>
                    <label className="parameter-label">
                      分块策略{" "}
                      <Help text="自动按资料结构选择；按句切分适合连续叙述；Markdown 层级适合有标题的资料。最终仍受模型 token 上限约束。token 是模型的计量单位，不等同字数。按原始元素独立切分，不跨页合并；短文调整长度时块数可能不变。" />
                    </label>
                    <Select
                      aria-label="分块策略"
                      disabled={disabled}
                      value={processing.chunking}
                      options={[
                        { value: "auto", label: "自动选择（默认）" },
                        { value: "sentence", label: "按句切分" },
                        { value: "markdown", label: "按 Markdown 层级" },
                      ].filter((item) =>
                        capabilities?.chunking.includes(item.value),
                      )}
                      onChange={(value: ProcessingOptions["chunking"]) =>
                        onProcessing({ ...processing, chunking: value })
                      }
                    />
                    <label className="parameter-label">索引增强</label>
                    <div className="parameter-options">
                      <div>
                        <Checkbox
                          disabled={disabled}
                          checked={processing.include_title}
                          onChange={(event) =>
                            onProcessing({
                              ...processing,
                              include_title: event.target.checked,
                            })
                          }
                        >
                          将标题加入索引
                        </Checkbox>
                        <Help text="默认关闭。开启后将资料标题加入片段的检索文本，帮助识别主题；会占用部分 token 预算，原文不被改写。" />
                      </div>
                      <div>
                        <Checkbox disabled>自动生成补充索引</Checkbox>
                        <small>
                          {capabilities?.supplemental_index.reason ||
                            "尚未接入生成模型与补充索引流程。"}
                        </small>
                      </div>
                      <div>
                        <Checkbox disabled>图片语义自动索引</Checkbox>
                        <small>
                          {capabilities?.image_semantics.reason ||
                            "尚未接入图片理解模型；OCR 文字仍可进入普通分块。"}
                        </small>
                      </div>
                    </div>
                    <label className="parameter-label">分块处理参数</label>
                    <Radio.Group
                      className="parameter-cards parameter-cards-stacked"
                      value={custom ? "custom" : "default"}
                      disabled={disabled}
                      onChange={(event) =>
                        onCustom(event.target.value === "custom")
                      }
                    >
                      <Radio value="default" className="parameter-card">
                        <strong>默认</strong>
                        <span>
                          使用服务默认块长度{" "}
                          {capabilities?.defaults?.chunk_size} token，重叠{" "}
                          {capabilities?.defaults?.chunk_overlap} token。
                        </span>
                      </Radio>
                      <Radio value="custom" className="parameter-card">
                        <strong>自定义</strong>
                        <span>
                          调整片段长度与相邻片段重叠，下一步查看实际效果。
                        </span>
                      </Radio>
                    </Radio.Group>
                    {custom && (
                      <div className="parameter-number-grid">
                        <div>
                          <label htmlFor="chunk-size">
                            块长度上限（token）
                          </label>
                          <InputNumber
                            id="chunk-size"
                            aria-label="块长度上限"
                            min={32}
                            max={capabilities?.max_tokens ?? undefined}
                            precision={0}
                            value={processing.chunk_size}
                            disabled={disabled}
                            onChange={(value) => {
                              if (value !== null)
                                onProcessing({
                                  ...processing,
                                  chunk_size: value,
                                });
                            }}
                          />
                          <small>
                            越大越保留上下文，越小越聚焦。当前模型上限{" "}
                            {capabilities?.max_tokens} token。
                          </small>
                        </div>
                        <div>
                          <label htmlFor="chunk-overlap">
                            相邻块重叠（token）
                          </label>
                          <InputNumber
                            id="chunk-overlap"
                            aria-label="相邻块重叠"
                            min={0}
                            max={Math.max(0, processing.chunk_size - 1)}
                            precision={0}
                            value={processing.chunk_overlap}
                            disabled={disabled}
                            onChange={(value) => {
                              if (value !== null)
                                onProcessing({
                                  ...processing,
                                  chunk_overlap: value,
                                });
                            }}
                          />
                          <small>
                            减少跨块断句造成的信息丢失；需小于块长度，较大值会增加重复内容。
                          </small>
                        </div>
                      </div>
                    )}
                  </>
                )}
              </>
            ),
          },
          {
            key: "clean",
            label: <h2>内容清洗</h2>,
            children: (
              <div className="parameter-options">
                <div>
                  <Checkbox
                    disabled={disabled}
                    checked={excludeHeaders}
                    onChange={(event) =>
                      onCleaning(event.target.checked, excludeImages)
                    }
                  >
                    排除页眉和页脚
                  </Checkbox>
                  <small>
                    默认关闭。只排除解析器已识别的页眉页脚，减少重复内容。
                  </small>
                </div>
                <div>
                  <Checkbox
                    disabled={disabled}
                    checked={excludeImages}
                    onChange={(event) =>
                      onCleaning(excludeHeaders, event.target.checked)
                    }
                  >
                    从清洗结果排除图片
                  </Checkbox>
                  <small>
                    默认关闭。不删除原件，也不删除已识别的 OCR 文字。
                  </small>
                </div>
              </div>
            ),
          },
        ]}
      />
    </div>
  );
}
