import { proxyPOSRequest } from "@/lib/pos-proxy";

export const dynamic = "force-dynamic";

type RouteContext = {
  params: Promise<{
    returnId: string;
  }>;
};

export async function GET(request: Request, context: RouteContext) {
  const { returnId } = await context.params;

  return proxyPOSRequest({
    endpoint: `/pos/returns/${encodeURIComponent(returnId)}`,
    request,
  });
}
