import { proxyPurchasesRequest } from "@/lib/purchases-proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  return proxyPurchasesRequest({
    endpoint: "/suppliers",
    request,
  });
}

export async function POST(request: Request) {
  return proxyPurchasesRequest({
    endpoint: "/suppliers",
    request,
    method: "POST",
  });
}
