import { proxyPurchasesRequest } from "@/lib/purchases-proxy";

export const dynamic = "force-dynamic";

type PurchaseRouteContext = {
  params: Promise<{
    id: string;
  }>;
};

async function getPurchaseEndpoint(context: PurchaseRouteContext) {
  const { id } = await context.params;
  return `/purchases/${encodeURIComponent(id)}`;
}

export async function GET(request: Request, context: PurchaseRouteContext) {
  return proxyPurchasesRequest({
    endpoint: await getPurchaseEndpoint(context),
    request,
  });
}

export async function PATCH(request: Request, context: PurchaseRouteContext) {
  return proxyPurchasesRequest({
    endpoint: await getPurchaseEndpoint(context),
    request,
    method: "PATCH",
  });
}
