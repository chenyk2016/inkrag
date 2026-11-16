/** 全站共享的企业工作台主题；业务页不另建一套按钮和表单皮肤。 */
import type { ThemeConfig } from "antd";

export const workspaceTheme: ThemeConfig = {
  token: {
    colorPrimary: "#1766e8",
    colorText: "#273244",
    colorTextSecondary: "#6d7788",
    colorBgLayout: "#f5f6f8",
    colorBgContainer: "#ffffff",
    colorBorder: "#dfe4ec",
    colorBorderSecondary: "#e8ecf2",
    borderRadius: 8,
    borderRadiusLG: 12,
    fontSize: 13,
    fontSizeSM: 12,
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", sans-serif',
    controlHeight: 32,
    controlHeightSM: 26,
    controlHeightLG: 38,
    boxShadowSecondary: "0 12px 40px #1728401a, 0 2px 8px #1728400d",
  },
  components: {
    Button: { fontWeight: 500, primaryShadow: "0 2px 4px #1766e819" },
    Table: { headerBg: "#f7f9fc", rowHoverBg: "#f3f7fd" },
    Tabs: { horizontalItemGutter: 24 },
    Modal: { titleFontSize: 16 },
    Tag: { borderRadiusSM: 5 },
  },
};
