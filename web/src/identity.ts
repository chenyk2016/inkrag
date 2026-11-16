/** 采用成熟OIDC代码流程与PKCE；访问令牌只留在内存。 */
import {
  InMemoryWebStorage,
  UserManager,
  WebStorageStateStore,
} from "oidc-client-ts";
import { object } from "./api";
let manager: Promise<UserManager> | null = null;
/** 公开配置没有秘密；仅瞬态PKCE状态存入会话存储以跨越身份跳转。 */
export function identity(): Promise<UserManager> {
  manager ??= (async () => {
    const response = await fetch("/api/v2/session/config", {
      cache: "no-store",
    });
    if (!response.ok) throw new Error("无法读取登录配置。");
    const config = object(await response.json());
    if (
      typeof config.issuer !== "string" ||
      typeof config.client_id !== "string"
    )
      throw new Error("登录配置不完整。");
    return new UserManager({
      authority: config.issuer,
      client_id: config.client_id,
      popup_redirect_uri: `${window.location.origin}/oidc/callback`,
      redirect_uri: `${window.location.origin}/oidc/callback`,
      silent_redirect_uri: `${window.location.origin}/oidc/silent-callback`,
      silentRequestTimeoutInSeconds: 5,
      post_logout_redirect_uri: window.location.origin,
      response_type: "code",
      scope: "openid profile",
      automaticSilentRenew: true,
      userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
      stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
    });
  })().catch((error: unknown) => {
    manager = null;
    throw error;
  });
  return manager;
}
