import { proxyPurchasesRequest } from "@/lib/purchases-proxy";

export const dynamic = "force-dynamic";

type PurchaseCancelRouteContext = {
  params: Promise<{
    id: string;
  }>;
};

async function getPurchaseCancelEndpoint(context: PurchaseCancelRouteContext) {
  const { id } = await context.params;
  return `/purchases/${encodeURIComponent(id)}/cancel`;
}

export async function POST(request: Request, context: PurchaseCancelRouteContext) {
  return proxyPurchasesRequest({
    endpoint: await getPurchaseCancelEndpoint(context),
    request,
    method: "POST",
  });
}
