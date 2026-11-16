/** 库内入口读取稳定身份，所有上传、审校和服务均限制在此库。 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { platformClient } from "./platform-api";
import { useWorkspace, Failure, PageFrame } from "./application";
import { Workbench } from "./workbench";
/** 直接按 ID 读取库，不以全局列表第一页作为授权或存在判断。 */
export default function LibraryRoute() {
  const { kbId } = useParams();
  const { token, capabilities } = useWorkspace(),
    navigate = useNavigate();
  const api = useMemo(() => platformClient(token, kbId ?? ""), [token, kbId]);
  const kb = useQuery({
    queryKey: ["knowledge-base", kbId],
    queryFn: () => api.schema(),
    enabled: Boolean(kbId),
    // 后台自动生效或其他页面发布后，库身份旁的状态也要更新。
    refetchInterval: 15_000,
  });
  // 后台刷新失败仍保留已有工作台，不能卸载并丢失未保存的草稿。
  if (!kb.data) {
    if (kb.isError)
      return (
        <PageFrame title="无法打开知识库" description="检查权限或返回工作台。">
          <Failure error={kb.error} retry={() => void kb.refetch()} />
        </PageFrame>
      );
    return <p role="status">正在读取知识库…</p>;
  }
  return (
    <>
      {kb.isError && (
        <p role="status">
          知识库状态暂未更新，正在显示上次结果；当前输入已保留。
          <button onClick={() => void kb.refetch()}>重试更新状态</button>
        </p>
      )}
      <Workbench
        key={kb.data.kb_id}
        token={token}
        capabilities={capabilities}
        knowledgeBase={kb.data}
        onChooseKnowledgeBase={() =>
          navigate(kb.data.kind === "parse" ? "/parsing" : "/knowledge")
        }
      />
    </>
  );
}
