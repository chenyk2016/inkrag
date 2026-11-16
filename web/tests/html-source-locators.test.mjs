import assert from "node:assert/strict";
import test from "node:test";
import { markHtmlSources } from "../src/html-source-locators.ts";

const marker = "data-inkrag-test";

test("HTML fragments use the original sibling order, including nested paragraph tags", () => {
  const input = "<p>前<p>内</p></p><p>后</p>";
  const paths = ["html:/p[1]", "html:/p[1]/p[1]", "html:/p[2]"];
  const { marked, positions } = markHtmlSources(input, marker, new Set(paths));
  assert.equal(
    marked,
    '<p data-inkrag-test="0">前<p data-inkrag-test="1">内</p></p><p data-inkrag-test="2">后</p>',
  );
  assert.deepEqual(
    [...positions.values()].map((entry) => entry.locator),
    paths,
  );
});

test("table groups and tag tokens count only when present in source", () => {
  const input = [
    "<!-- <table><tr><td><img></td></tr></table> -->",
    '<script>const demo = "<table>";</script>',
    '<section title="a > b"><table><tbody><tr><td><img alt="一">',
    '</td></tr></tbody><tr><td><img alt="二"></td></tr></table></section>',
  ].join("");
  const table = "html:/section[1]/table[1]";
  const paths = [
    table + "/tbody[1]/tr[1]/td[1]/img[1]",
    table + "/tr[1]/td[1]/img[1]",
  ];
  const { marked, positions } = markHtmlSources(input, marker, new Set(paths));
  assert.match(marked, /<img data-inkrag-test="0" alt="一">/u);
  assert.match(marked, /<img data-inkrag-test="1" alt="二">/u);
  assert.equal(marked.split(marker).length, 3);
  assert.deepEqual(
    [...positions.values()].map((entry) => entry.locator),
    paths,
  );
});

test("tags ignored by the parser and an over-budget source stay unmarked", () => {
  const paths = new Set(["html:/p[1]", "html:/template[1]/p[1]"]);
  const input = "<template><p>模板内容</p></template><p>可引用</p>";
  const { marked } = markHtmlSources(input, marker, paths);
  assert.equal(
    marked,
    '<template><p>模板内容</p></template><p data-inkrag-test="0">可引用</p>',
  );
  const deep = input + "<div>".repeat(130);
  assert.deepEqual(markHtmlSources(deep, marker, paths), {
    marked: deep,
    positions: new Map(),
  });
});
