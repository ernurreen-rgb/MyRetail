const DEFAULT_API_BASE_URL = "http://localhost:8000";
const ALLOWED_API_PROTOCOLS = new Set(["http:", "https:"]);
const ENDPOINT_CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/u;

function getValidatedApiBaseUrl() {
  // This is trusted deployment configuration, never request-derived input. Validation below
  // still fails closed so a malformed or unsafe deployment cannot redirect authenticated BFF calls.
  const configuredValue = process.env.MYRETAIL_API_URL ?? DEFAULT_API_BASE_URL;
  let url: URL;

  try {
    url = new URL(configuredValue);
  } catch {
    throw new Error("MYRETAIL_API_URL must be a valid absolute URL.");
  }

  if (!ALLOWED_API_PROTOCOLS.has(url.protocol)) {
    throw new Error("MYRETAIL_API_URL must use HTTP or HTTPS.");
  }
  if (url.username || url.password) {
    throw new Error("MYRETAIL_API_URL must not contain credentials.");
  }
  if (url.search || url.hash) {
    throw new Error("MYRETAIL_API_URL must not contain a query string or fragment.");
  }

  return url;
}

function validateApiEndpoint(endpoint: string) {
  if (
    !endpoint.startsWith("/") ||
    endpoint.startsWith("//") ||
    endpoint.includes("\\") ||
    endpoint.includes("?") ||
    endpoint.includes("#") ||
    ENDPOINT_CONTROL_CHARACTERS.test(endpoint)
  ) {
    throw new Error("MyRetail API endpoint must be an absolute path without an authority.");
  }

  for (const segment of endpoint.split("/")) {
    let decodedSegment: string;

    try {
      decodedSegment = decodeURIComponent(segment);
    } catch {
      throw new Error("MyRetail API endpoint contains invalid percent encoding.");
    }

    if (decodedSegment === "." || decodedSegment === "..") {
      throw new Error("MyRetail API endpoint must not contain dot path segments.");
    }
  }
}

export function getApiBaseUrl() {
  const url = getValidatedApiBaseUrl();
  const basePath = url.pathname.replace(/\/+$/, "");
  return `${url.origin}${basePath}`;
}

export function buildApiUrl(endpoint: string, searchParams?: URLSearchParams) {
  validateApiEndpoint(endpoint);

  const baseUrl = getValidatedApiBaseUrl();
  const basePath = baseUrl.pathname.replace(/\/+$/, "");
  const target = new URL(baseUrl.origin);
  target.pathname = `${basePath}${endpoint}`;

  for (const [key, value] of searchParams ?? []) {
    target.searchParams.append(key, value);
  }

  if (target.origin !== baseUrl.origin) {
    throw new Error("MyRetail API endpoint escaped the configured API origin.");
  }

  return target.toString();
}
