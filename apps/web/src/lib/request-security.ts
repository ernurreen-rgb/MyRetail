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

export function isSameOriginMutation(request: Request) {
  const expectedOrigin = getExpectedOrigin(request);
  const origin = request.headers.get("origin");

  if (!expectedOrigin || !origin) {
    return false;
  }

  try {
    if (new URL(origin).origin !== expectedOrigin) {
      return false;
    }
  } catch {
    return false;
  }

  const fetchSite = request.headers.get("sec-fetch-site");
  return fetchSite === null || fetchSite === "same-origin";
}
