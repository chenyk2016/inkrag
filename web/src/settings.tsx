/** 展示真实配置能力，不允许页面读取或修改后端存储凭证。 */
import { Alert, Tag } from "antd";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  CircleHelp,
  Code,
  Database,
  Settings2,
  ShieldCheck,
} from "lucide-react";
import type { Capabilities, Client, ServiceStatus } from "./api";
import { formatSize } from "./review";

/** 展示真实部署能力与接入边界，不在浏览器管理存储秘密。 */
export function Settings({
  capabilities,
  api,
}: {
  capabilities: Capabilities;
  api: Client;
}) {
  const parseOnly = capabilities.mode === "parse";
  const limitations = parseOnly
    ? capabilities.limitations.filter(
        (limitation) => !/入库|发布|检索/.test(limitation),
      )
    : capabilities.limitations;
  const [status, setStatus] = useState<ServiceStatus | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    void api
      .serviceStatus()
      .then((value) => {
        if (alive) setStatus(value);
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : "服务探测失败");
      });
    return () => {
      alive = false;
    };
  }, [api]);
  return (
    <div className="settings-page">
      <div className="section-intro">
        <div className="section-icon">
          <Settings2 size={23} />
        </div>
        <div>
          <h2>清楚地掌握你的服务</h2>
          <p>
            {parseOnly
              ? "页面和代码使用同一套解析、资产和版本管理能力。"
              : "页面和代码使用同一套解析、存储与检索能力。"}
          </p>
        </div>
      </div>
      <div className="settings-grid">
        <section className="settings-card">
          <h3>
            <Database size={18} /> 当前服务
          </h3>
          {error && <Alert type="error" showIcon title={error} />}
          <dl className="settings-list">
            <div>
              <dt>依赖探测</dt>
              <dd>
                <Tag
                  color={
                    status ? (status.ready ? "success" : "error") : "processing"
                  }
                >
                  {status
                    ? status.ready
                      ? "已就绪"
                      : "存在不可用依赖"
                    : "正在探测…"}
                </Tag>
              </dd>
            </div>
            <div>
              <dt>服务模式</dt>
              <dd>{parseOnly ? "独立文档解析" : capabilities.mode}</dd>
            </div>
            {!parseOnly && (
              <div>
                <dt>检索模型</dt>
                <dd>
                  {status?.model_identity ? (
                    <code>
                      {String(status.model_identity.revision ?? "未知revision")}{" "}
                      · {String(status.model_identity.dimensions ?? "未知")} 维
                    </code>
                  ) : (
                    "正在读取模型身份…"
                  )}
                </dd>
              </div>
            )}
            <div>
              <dt>接口地址</dt>
              <dd>
                <code>{window.location.origin}/api/v2</code>
              </dd>
            </div>
            <div>
              <dt>解析范围</dt>
              <dd>
                {[
                  ...new Set(
                    capabilities.parse_engines
                      .filter((engine) => engine.available)
                      .flatMap((engine) =>
                        engine.formats.filter(
                          (format) => !engine.pending_formats.includes(format),
                        ),
                      ),
                  ),
                ]
                  .map((format) => format.toUpperCase())
                  .join(" / ")}
              </dd>
            </div>
            <div>
              <dt>OCR 文字识别</dt>
              <dd>
                {capabilities.parse_engines
                  .filter(
                    (engine) =>
                      engine.available &&
                      engine.ocr_formats.some(
                        (format) => !engine.pending_formats.includes(format),
                      ),
                  )
                  .map(
                    (engine) =>
                      `${engine.name}：${engine.ocr_formats.filter((format) => !engine.pending_formats.includes(format)).join(" / ")}（默认${engine.ocr_default ? "开启" : "关闭"}）`,
                  )
                  .join("；") || "当前部署没有可用 OCR 工具"}
              </dd>
            </div>
            <div>
              <dt>图片内容理解</dt>
              <dd>尚未接入视觉模型；可查看原图并手动补充图片说明。</dd>
            </div>
            <div>
              <dt>单文件上限</dt>
              <dd>{formatSize(capabilities.max_upload_bytes)}</dd>
            </div>
            <div>
              <dt>存储连接</dt>
              <dd>{capabilities.storage}</dd>
            </div>
            <div>
              <dt>身份方式</dt>
              <dd>Bearer 访问令牌</dd>
            </div>
          </dl>
        </section>
        <section className="settings-card">
          <h3>
            <Code size={18} /> HTTP 接入
          </h3>
          <p>使用 Authorization 请求头访问同一个服务。</p>
          <pre>{`POST /api/v2/knowledge-bases/<kb-id>/documents?filename=manual.md\nAuthorization: Bearer <your-token>\nIdempotency-Key: <unique-operation-id>\nContent-Type: application/octet-stream\n\n<原始文件字节>`}</pre>
          <p className="small-note">
            存储由服务启动入口注入；MinIO 与 S3
            的凭证在服务器配置，页面不读取或保存这些凭证。
          </p>
        </section>
        <section className="settings-card full">
          <h3>
            <CircleHelp size={18} /> 能力与使用边界
          </h3>
          {limitations.length ? (
            <ul className="limitations">
              {limitations.map((limitation) => (
                <li key={limitation}>{limitation}</li>
              ))}
            </ul>
          ) : (
            <p>
              服务未返回额外能力说明。复杂格式的解析效果仍需用实际资料检查。
            </p>
          )}
          <Alert
            type="info"
            showIcon
            icon={<ShieldCheck size={18} />}
            title={
              parseOnly
                ? "原件与每次保存的解析版本独立保留。解析完成后可对照原文、修订文字、填写业务字段，并导出 JSON 或 Markdown。"
                : "上传的资料默认在清洗完成后自动生效。你可以在知识库设置中关闭自动生效。"
            }
          />
          {!parseOnly && (
            <p className="small-note">
              <Link to="/parsing">打开独立解析集合</Link>
              {" · "}查看、审校和导出无需入库的资料。
            </p>
          )}
        </section>
      </div>
    </div>
  );
}
