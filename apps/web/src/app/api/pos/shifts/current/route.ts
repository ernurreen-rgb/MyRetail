import { proxyPOSRequest } from "@/lib/pos-proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  return proxyPOSRequest({
    endpoint: "/pos/shifts/current",
    request,
  });
}
