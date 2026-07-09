import { proxyPOSRequest } from "@/lib/pos-proxy";

export const dynamic = "force-dynamic";

type RouteContext = {
  params: Promise<{
    shiftId: string;
  }>;
};

export async function POST(request: Request, context: RouteContext) {
  const { shiftId } = await context.params;

  return proxyPOSRequest({
    endpoint: `/pos/shifts/${encodeURIComponent(shiftId)}/close`,
    request,
    method: "POST",
  });
}
