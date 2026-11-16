import { Button, Input, Collapse } from "antd";
/** 使用一次真实能力查询建立内存中的管理会话。 */
import { useState, type FormEvent } from "react";
import {
  AlertCircle,
  ArrowRight,
  ChevronRight,
  Cloud,
  FileSearch,
  FileText,
  KeyRound,
  Layers,
  ShieldCheck,
} from "lucide-react";
import { client, type Capabilities } from "./api";

/** 把外部异常转为可展示文本，未知异常不输出对象内容。 */
function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "连接未完成，请重试。";
}

/** 建立同源管理会话，只有服务确认令牌有效后才进入工作台。 */
export function Connect({
  onConnect,
  onSignIn,
  errorMessage: loginError,
}: {
  onConnect: (token: string, capabilities: Capabilities) => void;
  onSignIn: () => void;
  errorMessage: string;
}) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  /** 检查真实服务能力，令牌不会保存到浏览器存储或 URL。 */
  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busy || !token.trim()) return;
    setBusy(true);
    setError("");
    try {
      onConnect(token.trim(), await client(token.trim()).capabilities());
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="connect-page">
      <div className="connect-brand">
        <div className="brand-symbol">
          <Layers size={26} />
        </div>
        <span>
          InkRAG <span>工作台</span>
        </span>
      </div>
      <div className="connect-layout">
        <section className="connect-story">
          <div className="eyebrow">DOCUMENTS, CONNECTED.</div>
          <h1>
            让资料成为
            <br />
            可以信任的知识。
          </h1>
          <p>解析、检查、入库与回查，在同一个清晰的工作流中完成。</p>
          <div className="connect-flow">
            <span>
              <FileText size={19} /> 解析资料
            </span>
            <ChevronRight size={16} />
            <span>
              <ShieldCheck size={19} /> 检查修订
            </span>
            <ChevronRight size={16} />
            <span>
              <FileSearch size={19} /> 检索回查
            </span>
          </div>
          <div className="connect-note">
            <Cloud size={17} />
            <div>
              独立解析 · 可替换存储 · 可追溯检索
              <small>原件、图片和每次解析结果保持关联。</small>
            </div>
          </div>
        </section>
        <section className="connect-card">
          <div className="lock-icon">
            <KeyRound size={25} />
          </div>
          <h2>连接你的工作台</h2>
          <p>使用企业账号登录，进入你有权限的知识库。</p>
          <Button type="primary" className="connect-button" onClick={onSignIn}>
            使用企业账号登录 <ArrowRight size={16} />
          </Button>
          {loginError && (
            <p className="connect-error" role="alert">
              {loginError}
            </p>
          )}
          <Collapse
            className="developer-login"
            items={[
              {
                key: "token",
                label: "使用已有 OIDC / 调用方令牌",
                children: (
                  <form onSubmit={(event) => void submit(event)}>
                    <label htmlFor="access-token">访问令牌</label>
                    <div className="token-field">
                      <KeyRound size={16} />
                      <Input.Password
                        id="access-token"
                        autoComplete="off"
                        value={token}
                        onChange={(event) => setToken(event.target.value)}
                        required
                        disabled={busy}
                        placeholder="粘贴你的访问令牌"
                      />
                    </div>
                    {error && (
                      <div className="connect-error" role="alert">
                        <AlertCircle size={15} />
                        {error}
                      </div>
                    )}
                    <Button
                      type="primary"
                      className="connect-button"
                      htmlType="submit"
                      loading={busy}
                      disabled={busy || !token.trim()}
                    >
                      {busy ? "正在连接…" : "连接工作台"}
                      <ArrowRight size={16} />
                    </Button>
                  </form>
                ),
              },
            ]}
          />
          <div className="privacy-note">
            <ShieldCheck size={14} />
            访问令牌保存在当前页面内存中，身份由企业登录服务验证。
          </div>
        </section>
      </div>
      <footer className="connect-footer">
        InkParse <span>+</span> InkStore <span>+</span> InkRAG <i>·</i>{" "}
        资料处理与知识检索
      </footer>
    </main>
  );
}
