import { proxyProductRequest } from "@/lib/products-proxy";

export const dynamic = "force-dynamic";

type ProductRouteContext = {
  params: Promise<{
    id: string;
  }>;
};

async function getProductEndpoint(context: ProductRouteContext) {
  const { id } = await context.params;
  return `/products/${encodeURIComponent(id)}`;
}

export async function GET(request: Request, context: ProductRouteContext) {
  return proxyProductRequest({
    endpoint: await getProductEndpoint(context),
    request,
  });
}

export async function PATCH(request: Request, context: ProductRouteContext) {
  return proxyProductRequest({
    endpoint: await getProductEndpoint(context),
    request,
    method: "PATCH",
  });
}

export async function DELETE(request: Request, context: ProductRouteContext) {
  return proxyProductRequest({
    endpoint: await getProductEndpoint(context),
    request,
    method: "DELETE",
  });
}
