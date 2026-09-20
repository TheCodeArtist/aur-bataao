import assert from "node:assert/strict";
import { test } from "node:test";

import { requestJson } from "../static/http.js";


function response({ ok = true, status = 200, body = {}, jsonError = null } = {}) {
  return {
    ok,
    status,
    async json() {
      if (jsonError) throw jsonError;
      return body;
    },
  };
}


test("requestJson validates its browser dependencies", async () => {
  await assert.rejects(
    requestJson("/api", {}, {}),
    /Browser networking is unavailable/,
  );
});


test("requestJson sends JSON defaults and returns parsed responses", async () => {
  let received;
  const environment = {
    Headers,
    FormData,
    fetch: async (url, options) => {
      received = { url, options };
      return response({ body: { value: 1 } });
    },
  };
  assert.deepEqual(
    await requestJson("/api/value", { method: "POST", body: "{}" }, environment),
    { value: 1 },
  );
  assert.equal(received.url, "/api/value");
  assert.equal(received.options.headers.get("Content-Type"), "application/json");

  await requestJson(
    "/api/value",
    { body: "text", headers: { "Content-Type": "text/plain" } },
    environment,
  );
  assert.equal(received.options.headers.get("Content-Type"), "text/plain");

  await requestJson("/upload", { method: "POST", body: new FormData() }, environment);
  assert.equal(received.options.headers.has("Content-Type"), false);
});


test("requestJson handles empty, invalid, and failed responses", async () => {
  const environment = { Headers, FormData, fetch: async () => response({ status: 204 }) };
  assert.equal(await requestJson("/empty", {}, environment), null);

  environment.fetch = async () => response({ ok: false, status: 204 });
  await assert.rejects(requestJson("/empty", {}, environment), /Request failed \(204\)/);

  environment.fetch = async () => response({ jsonError: new SyntaxError("bad") });
  await assert.rejects(requestJson("/bad", {}, environment), /invalid response/);

  environment.fetch = async () => response({
    ok: false,
    status: 400,
    body: { error: "Specific failure" },
  });
  await assert.rejects(requestJson("/bad", {}, environment), /Specific failure/);

  environment.fetch = async () => response({
    ok: false,
    status: 503,
    jsonError: new SyntaxError("bad"),
  });
  await assert.rejects(requestJson("/bad", {}, environment), /Request failed \(503\)/);

  environment.fetch = async () => response({
    ok: false,
    status: 422,
    body: { error: 42 },
  });
  await assert.rejects(requestJson("/bad", {}, environment), /Request failed \(422\)/);
});
