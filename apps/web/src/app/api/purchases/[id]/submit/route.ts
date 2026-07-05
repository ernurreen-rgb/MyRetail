import { proxyPurchasesRequest } from "@/lib/purchases-proxy";

export const dynamic = "force-dynamic";

type PurchaseSubmitRouteContext = {
  params: Promise<{
    id: string;
  }>;
};

async function getPurchaseSubmitEndpoint(context: PurchaseSubmitRouteContext) {
  const { id } = await context.params;
  return `/purchases/${encodeURIComponent(id)}/submit`;
}

export async function POST(request: Request, context: PurchaseSubmitRouteContext) {
  return proxyPurchasesRequest({
    endpoint: await getPurchaseSubmitEndpoint(context),
    request,
    method: "POST",
  });
}
