import { proxyPurchasesRequest } from "@/lib/purchases-proxy";

export const dynamic = "force-dynamic";

type SupplierRouteContext = {
  params: Promise<{
    id: string;
  }>;
};

async function getSupplierEndpoint(context: SupplierRouteContext) {
  const { id } = await context.params;
  return `/suppliers/${encodeURIComponent(id)}`;
}

export async function GET(request: Request, context: SupplierRouteContext) {
  return proxyPurchasesRequest({
    endpoint: await getSupplierEndpoint(context),
    request,
  });
}

export async function PATCH(request: Request, context: SupplierRouteContext) {
  return proxyPurchasesRequest({
    endpoint: await getSupplierEndpoint(context),
    request,
    method: "PATCH",
  });
}

export async function DELETE(request: Request, context: SupplierRouteContext) {
  return proxyPurchasesRequest({
    endpoint: await getSupplierEndpoint(context),
    request,
    method: "DELETE",
  });
}
