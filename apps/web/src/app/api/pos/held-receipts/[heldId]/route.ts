import { proxyPOSRequest } from "@/lib/pos-proxy";

export const dynamic = "force-dynamic";

type RouteContext = {
  params: Promise<{
    heldId: string;
  }>;
};

export async function GET(request: Request, context: RouteContext) {
  const { heldId } = await context.params;

  return proxyPOSRequest({
    endpoint: `/pos/held-receipts/${encodeURIComponent(heldId)}`,
    request,
  });
}

export async function PATCH(request: Request, context: RouteContext) {
  const { heldId } = await context.params;

  return proxyPOSRequest({
    endpoint: `/pos/held-receipts/${encodeURIComponent(heldId)}`,
    request,
    method: "PATCH",
  });
}

export async function DELETE(request: Request, context: RouteContext) {
  const { heldId } = await context.params;

  return proxyPOSRequest({
    endpoint: `/pos/held-receipts/${encodeURIComponent(heldId)}`,
    request,
    method: "DELETE",
  });
}
