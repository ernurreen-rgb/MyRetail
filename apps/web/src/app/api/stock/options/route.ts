import { proxyStockRequest } from "@/lib/stock-proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  return proxyStockRequest({
    endpoint: "/stock/options",
    request,
  });
}
