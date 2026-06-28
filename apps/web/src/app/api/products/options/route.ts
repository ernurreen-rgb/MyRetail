import { proxyProductRequest } from "@/lib/products-proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  return proxyProductRequest({
    endpoint: "/products/options",
    request,
  });
}
