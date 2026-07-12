import { beforeEach, describe, expect, it, vi } from "vitest";

const routeDependencies = vi.hoisted(() => ({
  proxyProductRequest: vi.fn(),
}));

vi.mock("@/lib/products-proxy", () => ({
  proxyProductRequest: routeDependencies.proxyProductRequest,
}));

import { DELETE as productDELETE, GET as productGET, PATCH as productPATCH } from "@/app/api/products/[id]/route";
import { GET as optionsGET } from "@/app/api/products/options/route";
import { GET as productsGET, POST as productsPOST } from "@/app/api/products/route";

beforeEach(() => {
  routeDependencies.proxyProductRequest.mockReset().mockResolvedValue(new Response(null));
});

describe("product route handlers", () => {
  it("forwards product collection and options routes through the BFF proxy", async () => {
    const request = new Request("http://localhost:3000/api/products?q=milk");

    await productsGET(request);
    expect(routeDependencies.proxyProductRequest).toHaveBeenLastCalledWith({
      endpoint: "/products",
      request,
    });

    await productsPOST(request);
    expect(routeDependencies.proxyProductRequest).toHaveBeenLastCalledWith({
      endpoint: "/products",
      request,
      method: "POST",
    });

    await optionsGET(request);
    expect(routeDependencies.proxyProductRequest).toHaveBeenLastCalledWith({
      endpoint: "/products/options",
      request,
    });
  });

  it("encodes product ids for detail, update and archive routes", async () => {
    const request = new Request("http://localhost:3000/api/products/QA%20MILK");
    const params = { params: Promise.resolve({ id: "QA MILK" }) };

    await productGET(request, params);
    expect(routeDependencies.proxyProductRequest).toHaveBeenLastCalledWith({
      endpoint: "/products/QA%20MILK",
      request,
    });

    await productPATCH(request, params);
    expect(routeDependencies.proxyProductRequest).toHaveBeenLastCalledWith({
      endpoint: "/products/QA%20MILK",
      request,
      method: "PATCH",
    });

    await productDELETE(request, params);
    expect(routeDependencies.proxyProductRequest).toHaveBeenLastCalledWith({
      endpoint: "/products/QA%20MILK",
      request,
      method: "DELETE",
    });
  });
});
