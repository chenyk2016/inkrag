/** OIDC登录与刷新保留在内存，身份过期时保留当前页面草稿。 */
import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { client, type Capabilities } from "./api";
import { Connect } from "./connect";
import { Application } from "./application";
import { App as AntApp, Button, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import "antd/dist/reset.css";
import { workspaceTheme } from "./ui-theme";
import { identity } from "./identity";
import "./styles.css";
import "./application.css";
interface Session {
  token: string;
  capabilities: Capabilities;
  subject: string | null;
}
const signedOutKey = "inkrag:signed-out";
/** 成熟OIDC库处理code、nonce、PKCE与内存refresh token，页面不管理密码。 */
function App() {
  const [session, setSession] = useState<Session | null>(null),
    [loading, setLoading] = useState(true),
    [error, setError] = useState(""),
    [sessionWarning, setSessionWarning] = useState("");
  useEffect(() => {
    const callback = window.location.pathname === "/oidc/callback";
    let active = true;
    void identity()
      .then((manager) =>
        callback
          ? manager.signinCallback()
          : sessionStorage.getItem(signedOutKey)
            ? null
            : manager.signinSilent(),
      )
      .then(async (user) => {
        const state = user?.state;
        const target =
          typeof state === "object" &&
          state !== null &&
          "returnTo" in state &&
          typeof state.returnTo === "string" &&
          state.returnTo.startsWith("/") &&
          !state.returnTo.startsWith("//")
            ? state.returnTo
            : "/";
        if (callback) window.history.replaceState({}, "", target);
        if (!user) return;
        const capabilities = await client(user.access_token).capabilities();
        if (active) {
          sessionStorage.removeItem(signedOutKey);
          setSession({
            token: user.access_token,
            capabilities,
            subject: user.profile.sub,
          });
        }
      })
      .catch((reason: unknown) => {
        if (active && callback)
          setError(reason instanceof Error ? reason.message : "登录失败");
      })
      .finally(() => {
        if (window.location.pathname === "/oidc/callback")
          window.history.replaceState({}, "", "/");
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    let alive = true;
    const remove: (() => void)[] = [];
    void identity()
      .then((manager) => {
        if (!alive) return;
        remove.push(
          manager.events.addUserLoaded((user) => {
            void client(user.access_token)
              .capabilities()
              .then((capabilities) => {
                if (alive) {
                  setSession((before) =>
                    before
                      ? {
                          token: user.access_token,
                          capabilities,
                          subject: user.profile.sub,
                        }
                      : before,
                  );
                  setSessionWarning("");
                }
              })
              .catch(() => {
                if (alive)
                  setSessionWarning(
                    "身份刷新未通过服务校验。当前草稿保留，可重新登录后继续保存。",
                  );
              });
          }),
          manager.events.addSilentRenewError(() => {
            if (alive)
              setSessionWarning(
                "会话自动续期失败，当前草稿仍保留。请重新登录后继续保存。",
              );
          }),
          manager.events.addAccessTokenExpired(() => {
            if (alive)
              setSessionWarning(
                "会话已过期，服务器将拒绝新的操作。当前草稿仍保留，请重新登录。",
              );
          }),
        );
      })
      .catch(() => undefined);
    return () => {
      alive = false;
      remove.forEach((cleanup) => cleanup());
    };
  }, []);
  /** 首次登录允许正常跳转；重新认证用弹窗保留当前审校内存。 */
  async function signIn(popup = false): Promise<void> {
    setError("");
    try {
      sessionStorage.removeItem(signedOutKey);
      const manager = await identity();
      if (popup) {
        const user = await manager.signinPopup();
        const capabilities = await client(user.access_token).capabilities();
        setSession({
          token: user.access_token,
          capabilities,
          subject: user.profile.sub,
        });
        setSessionWarning("");
      } else
        await manager.signinRedirect({
          state: {
            returnTo:
              window.location.pathname +
              window.location.search +
              window.location.hash,
          },
        });
    } catch (reason: unknown) {
      const message =
        reason instanceof Error ? reason.message : "无法打开登录页面";
      if (popup) setSessionWarning(`重新登录未完成，草稿仍保留。${message}`);
      else setError(message);
    }
  }
  /** 退出包含身份服务会话；失败时仍清空本页授权。 */
  function disconnect(): void {
    sessionStorage.setItem(signedOutKey, "1");
    window.history.replaceState({}, "", "/");
    setSession(null);
    setSessionWarning("");
    void identity()
      .then((manager) => manager.signoutRedirect())
      .catch(() => {
        setError("已退出本页，身份服务退出未完成，可关闭浏览器会话。");
      });
  }
  return loading ? (
    <main className="knowledge-picker">
      <p role="status">正在验证登录…</p>
    </main>
  ) : session ? (
    <>
      {sessionWarning && (
        <div className="session-warning" role="alert">
          <span>{sessionWarning}</span>
          <Button onClick={() => void signIn(true)}>重新登录并保留草稿</Button>
        </div>
      )}
      <Application
        key={`${session.capabilities.tenant_id}:${session.subject ?? "manual"}`}
        token={session.token}
        capabilities={session.capabilities}
        onDisconnect={disconnect}
      />
    </>
  ) : (
    <Connect
      errorMessage={error}
      onSignIn={() => void signIn()}
      onConnect={(token, capabilities) =>
        setSession({ token, capabilities, subject: null })
      }
    />
  );
}
if (window.location.pathname === "/oidc/silent-callback") {
  // 仅把授权响应交还给发起检查的父窗口；此页不挂载工作台。
  if (window.parent !== window)
    void identity()
      .then((manager) => manager.signinSilentCallback())
      .catch(() => undefined);
} else {
  const root = document.getElementById("root");
  if (!root) throw new Error("页面缺少应用挂载点。");
  createRoot(root).render(
    <ConfigProvider locale={zhCN} theme={workspaceTheme}>
      <AntApp className="workspace-framework">
        <App />
      </AntApp>
    </ConfigProvider>,
  );
}
