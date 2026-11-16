/** 生成带内容摘要的正式静态资源，并完整打包进 Python 分发物。 */
import { build } from "vite";
import { cp, mkdir, rm } from "node:fs/promises";
await build();
const target = "../packages/inkrag-platform/src/inkrag_platform/static";
await rm(target, { recursive: true, force: true });
await mkdir(target, { recursive: true });
await cp("dist", target, { recursive: true });
console.log("正式界面已构建并装入平台分发包。");
