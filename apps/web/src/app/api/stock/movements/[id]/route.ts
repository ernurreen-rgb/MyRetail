import { proxyStockRequest } from "@/lib/stock-proxy";

export const dynamic = "force-dynamic";

type StockMovementRouteContext = {
  params: Promise<{
    id: string;
  }>;
};

async function getMovementEndpoint(context: StockMovementRouteContext) {
  const { id } = await context.params;
  return `/stock/movements/${encodeURIComponent(id)}`;
}

export async function GET(request: Request, context: StockMovementRouteContext) {
  return proxyStockRequest({
    endpoint: await getMovementEndpoint(context),
    request,
  });
}
