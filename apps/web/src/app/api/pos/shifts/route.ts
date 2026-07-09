import { proxyPOSRequest } from "@/lib/pos-proxy";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  return proxyPOSRequest({
    endpoint: "/pos/shifts",
    request,
    method: "POST",
  });
}
