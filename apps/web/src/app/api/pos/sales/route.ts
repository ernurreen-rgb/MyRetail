import { proxyPOSRequest } from "@/lib/pos-proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  return proxyPOSRequest({
    endpoint: "/pos/sales",
    request,
  });
}

export async function POST(request: Request) {
  return proxyPOSRequest({
    endpoint: "/pos/sales",
    request,
    method: "POST",
  });
}
