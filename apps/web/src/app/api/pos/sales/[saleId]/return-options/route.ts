import { proxyPOSRequest } from "@/lib/pos-proxy";

export const dynamic = "force-dynamic";

type RouteContext = {
  params: Promise<{
    saleId: string;
  }>;
};

export async function GET(request: Request, context: RouteContext) {
  const { saleId } = await context.params;

  return proxyPOSRequest({
    endpoint: `/pos/sales/${encodeURIComponent(saleId)}/return-options`,
    request,
  });
}
