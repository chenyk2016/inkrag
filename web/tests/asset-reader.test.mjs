import assert from "node:assert/strict";
import test from "node:test";
import { createAssetReader } from "../src/asset-reader.ts";

test("six visible resources share two download slots", async () => {
  const started = [];
  const release = [];
  const reader = createAssetReader(
    async (id) => {
      started.push(id);
      await new Promise((resolve) => release.push(resolve));
      return new Blob([id]);
    },
    () => null,
  );
  const pending = Array.from({ length: 6 }, (_, i) => reader(String(i)));
  assert.deepEqual(started, ["0", "1"]);
  for (let i = 0; i < 6; i += 2) {
    release[i]();
    release[i + 1]();
    await new Promise(setImmediate);
    assert.equal(started.length, Math.min(i + 4, 6));
  }
  assert.deepEqual(
    await Promise.all((await Promise.all(pending)).map((blob) => blob.text())),
    ["0", "1", "2", "3", "4", "5"],
  );
});

test("one consumer can cancel without stopping a shared read", async () => {
  let finish;
  let count = 0;
  const reader = createAssetReader(
    async (id, signal) => {
      count += 1;
      await new Promise((resolve) => {
        finish = resolve;
      });
      assert.equal(signal.aborted, false);
      return new Blob([id]);
    },
    () => null,
  );
  const first = new AbortController();
  const cancelled = reader("same", first.signal);
  const other = reader("same");
  first.abort();
  await assert.rejects(cancelled, { name: "AbortError" });
  finish();
  assert.equal(await (await other).text(), "same");
  assert.equal(count, 1);
});

test("only capacity errors retry and a queued cancellation skips the fetch", async () => {
  const capacity = new Error("capacity");
  const forbidden = new Error("forbidden");
  const seen = [];
  const reader = createAssetReader(
    async (id) => {
      seen.push(id);
      if (id === "held")
        await new Promise((resolve) => setTimeout(resolve, 10));
      if (id === "forbidden") throw forbidden;
      if (
        id === "retry" &&
        seen.filter((item) => item === "retry").length === 1
      )
        throw capacity;
      return new Blob([id]);
    },
    (error) => (error === capacity ? 0 : null),
  );
  const held = [reader("held"), reader("forbidden")];
  const queued = new AbortController();
  const cancelled = reader("queued", queued.signal);
  queued.abort();
  await assert.rejects(cancelled, { name: "AbortError" });
  await assert.rejects(held[1], forbidden);
  await held[0];
  assert.equal(await (await reader("retry")).text(), "retry");
  assert.deepEqual(seen, ["held", "forbidden", "retry", "retry"]);
});
