export async function requestJson(url, options = {}, environment = globalThis) {
  const HeadersType = environment.Headers;
  const FormDataType = environment.FormData;
  const fetchRequest = environment.fetch;
  if (typeof HeadersType !== "function" || typeof fetchRequest !== "function") {
    throw new Error("Browser networking is unavailable");
  }

  const headers = new HeadersType(options.headers || {});
  const isFormData = typeof FormDataType === "function" && options.body instanceof FormDataType;
  if (options.body && !isFormData && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetchRequest(url, { ...options, headers });
  if (response.status === 204) {
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    return null;
  }

  let body = null;
  try {
    body = await response.json();
  } catch (_) {
    if (response.ok) throw new Error("The server returned an invalid response");
  }
  if (!response.ok) {
    const message = body && typeof body.error === "string" && body.error
      ? body.error
      : `Request failed (${response.status})`;
    throw new Error(message);
  }
  return body;
}
