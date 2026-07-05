import { proxyPurchasesRequest } from "@/lib/purchases-proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  return proxyPurchasesRequest({
    endpoint: "/purchases",
    request,
  });
}

export async function POST(request: Request) {
  return proxyPurchasesRequest({
    endpoint: "/purchases",
    request,
    method: "POST",
  });
}
