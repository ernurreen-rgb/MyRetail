const ALLOWED_ORIGIN_PROTOCOLS = new Set(["http:", "https:"]);

function parseConfiguredOrigin(configuredOrigin: string) {
  try {
    const url = new URL(configuredOrigin);

    if (
      !ALLOWED_ORIGIN_PROTOCOLS.has(url.protocol) ||
      url.username ||
      url.password ||
      url.pathname !== "/" ||
      url.search ||
      url.hash ||
      (process.env.NODE_ENV === "production" && url.protocol !== "https:")
    ) {
      return null;
    }

    return url.origin;
  } catch {
    return null;
  }
}

export function getExpectedOrigin(request: Request) {
  const configuredOrigin = process.env.MYRETAIL_WEB_ORIGIN?.trim();

  if (configuredOrigin) {
    return parseConfiguredOrigin(configuredOrigin);
  }

  if (process.env.NODE_ENV === "production") {
    return null;
  }

  try {
    return new URL(request.url).origin;
  } catch {
    return null;
  }
}

function isDevelopmentLoopbackAlias(origin: URL, expectedOrigin: URL) {
  if (process.env.NODE_ENV === "production") {
    return false;
  }

  const loopbackHosts = new Set(["localhost", "127.0.0.1", "[::1]"]);

  return (
    loopbackHosts.has(origin.hostname) &&
    loopbackHosts.has(expectedOrigin.hostname) &&
    origin.protocol === expectedOrigin.protocol &&
    origin.port === expectedOrigin.port
  );
}

export function getVerifiedRequestOrigin(request: Request) {
  const expectedOrigin = getExpectedOrigin(request);
  const origin = request.headers.get("origin");

  if (!expectedOrigin || !origin) {
    return null;
  }

  try {
    const parsedOrigin = new URL(origin);
    const parsedExpectedOrigin = new URL(expectedOrigin);

    if (
      parsedOrigin.username ||
      parsedOrigin.password ||
      parsedOrigin.pathname !== "/" ||
      parsedOrigin.search ||
      parsedOrigin.hash
    ) {
      return null;
    }

    if (
      parsedOrigin.origin !== parsedExpectedOrigin.origin &&
      !isDevelopmentLoopbackAlias(parsedOrigin, parsedExpectedOrigin)
    ) {
      return null;
    }

    const fetchSite = request.headers.get("sec-fetch-site");
    return fetchSite === null || fetchSite === "same-origin" ? parsedOrigin.origin : null;
  } catch {
    return null;
  }
}

export function isSameOriginMutation(request: Request) {
  return getVerifiedRequestOrigin(request) !== null;
}
