export function getExpectedOrigin(request: Request) {
  const configuredOrigin = process.env.MYRETAIL_WEB_ORIGIN?.trim();

  if (configuredOrigin) {
    try {
      return new URL(configuredOrigin).origin;
    } catch {
      return null;
    }
  }

  return new URL(request.url).origin;
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
