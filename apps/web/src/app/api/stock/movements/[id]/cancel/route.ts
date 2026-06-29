import { proxyStockRequest } from "@/lib/stock-proxy";

export const dynamic = "force-dynamic";

type StockMovementCancelRouteContext = {
  params: Promise<{
    id: string;
  }>;
};

async function getMovementCancelEndpoint(context: StockMovementCancelRouteContext) {
  const { id } = await context.params;
  return `/stock/movements/${encodeURIComponent(id)}/cancel`;
}

export async function POST(request: Request, context: StockMovementCancelRouteContext) {
  return proxyStockRequest({
    endpoint: await getMovementCancelEndpoint(context),
    request,
    method: "POST",
  });
}
