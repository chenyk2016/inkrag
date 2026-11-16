/** 给原件中的显式标签临时标记解析路径，独立于浏览器补全或重排的 DOM。 */
import { Tokenizer } from "htmlparser2";

// 与内置解析使用的 BeautifulSoup HTMLTreeBuilder 空元素集合一致。
const emptyTags = new Set(
  "area base basefont bgsound br col command embed frame hr image img input isindex keygen link menuitem meta nextid param source spacer track wbr".split(
    " ",
  ),
);
const removedTags = new Set([
  "script",
  "style",
  "noscript",
  "template",
  "head",
]);

interface SourceTag {
  name: string;
  path: string;
  children: Map<string, number>;
  removed: boolean;
}

/** 只标记解析结果引用的标签，HTML 词法由 htmlparser2 提供，路径沿用内置解析的显式标签栈。 */
export function markHtmlSources(
  content: string,
  marker: string,
  locators: ReadonlySet<string>,
): {
  marked: string;
  positions: Map<string, { name: string; locator: string }>;
} {
  const positions = new Map<string, { name: string; locator: string }>();
  if (!locators.size) return { marked: content, positions };

  const root = { name: "", path: "html:", children: new Map(), removed: false };
  const stack: SourceTag[] = [root];
  const pieces: string[] = [];
  let copied = 0;
  let name = "";
  let nameStart = 0;
  let nameEnd = 0;
  let overBudget = false;

  /** 标签结束时记录同名序号，遇到超深预览时保留原件并取消定位。 */
  function openTag(selfClosing: boolean): void {
    if (overBudget) return;
    const parent = stack[stack.length - 1];
    const ordinal = (parent.children.get(name) ?? 0) + 1;
    parent.children.set(name, ordinal);
    const removed = parent.removed || removedTags.has(name);
    const path = `${parent.path}/${name}[${ordinal}]`;
    if (!removed && locators.has(path)) {
      const id = String(positions.size);
      positions.set(id, { name, locator: path });
      // 在标签名和后续属性之间插入标记，以便浏览器移动节点时仍能核对其来源。
      pieces.push(content.slice(copied, nameEnd), ` ${marker}="${id}"`);
      copied = nameEnd;
    }
    if (!selfClosing && !emptyTags.has(name)) {
      if (stack.length > 128) overBudget = true;
      else stack.push({ name, path, children: new Map(), removed });
    }
  }

  /** 非结构事件由 tokenizer 消费，文本、属性、注释和声明原样交给安全净化。 */
  function ignore(): void {}

  const tokenizer = new Tokenizer(
    { decodeEntities: false },
    {
      onopentagname(start, end) {
        nameStart = start;
        name = content.slice(start, end).toLowerCase();
        nameEnd = end;
      },
      onopentagend() {
        if (content[nameStart - 1] === "<") openTag(false);
      },
      onselfclosingtag(end) {
        // HTMLParser 的显式自闭合要求斜线紧邻 >。浏览器宽松接受的形式仍用原件栈。
        if (content[nameStart - 1] === "<") openTag(content[end - 1] === "/");
      },
      onclosetag(start, end) {
        if (overBudget) return;
        const closing = content.slice(start, end).toLowerCase();
        for (let index = stack.length - 1; index > 0; index--) {
          if (stack[index].name !== closing) continue;
          stack.length = index;
          break;
        }
      },
      onattribdata: ignore,
      onattribentity: ignore,
      onattribend: ignore,
      onattribname: ignore,
      oncdata: ignore,
      oncomment: ignore,
      ondeclaration: ignore,
      onend: ignore,
      onprocessinginstruction: ignore,
      ontext: ignore,
      ontextentity: ignore,
    },
  );
  tokenizer.write(content);
  tokenizer.end();
  if (overBudget) return { marked: content, positions: new Map() };
  pieces.push(content.slice(copied));
  return { marked: pieces.join(""), positions };
}
