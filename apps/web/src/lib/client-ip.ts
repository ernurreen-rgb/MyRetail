import { isIP } from "node:net";

const MAX_IP_ADDRESS_LENGTH = 45;

export function getAlbForwardedClientIp(request: Request) {
  const forwardedFor = request.headers.get("x-forwarded-for");
  if (!forwardedFor) {
    return null;
  }

  // Production ALB is pinned to append mode without client ports. The last
  // value is therefore the hop added by ALB; user-supplied prefixes are not
  // forwarded into the private API trust boundary.
  const separator = forwardedFor.lastIndexOf(",");
  const candidate = forwardedFor.slice(separator + 1).trim();
  if (
    candidate.length === 0 ||
    candidate.length > MAX_IP_ADDRESS_LENGTH ||
    isIP(candidate) === 0
  ) {
    return null;
  }

  return candidate;
}
