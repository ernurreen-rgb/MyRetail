import { beforeEach, describe, expect, it, vi } from "vitest";

const routeDependencies = vi.hoisted(() => ({
  proxyPurchasesRequest: vi.fn(),
}));

vi.mock("@/lib/purchases-proxy", () => ({
  proxyPurchasesRequest: routeDependencies.proxyPurchasesRequest,
}));

import { GET as purchasesGET, POST as purchasesPOST } from "@/app/api/purchases/route";
import { GET as purchaseOptionsGET } from "@/app/api/purchases/options/route";
import { GET as purchaseGET, PATCH as purchasePATCH } from "@/app/api/purchases/[id]/route";
import { POST as purchaseSubmitPOST } from "@/app/api/purchases/[id]/submit/route";
import { POST as purchaseCancelPOST } from "@/app/api/purchases/[id]/cancel/route";
import { GET as suppliersGET, POST as suppliersPOST } from "@/app/api/suppliers/route";
import {
  DELETE as supplierDELETE,
  GET as supplierGET,
  PATCH as supplierPATCH,
} from "@/app/api/suppliers/[id]/route";

beforeEach(() => {
  routeDependencies.proxyPurchasesRequest.mockReset().mockResolvedValue(new Response(null));
});

describe("purchases route handlers", () => {
  it("forwards collection routes to the purchases proxy", async () => {
    const request = new Request("http://localhost:3000/api/purchases?q=milk");

    await purchasesGET(request);
    expect(routeDependencies.proxyPurchasesRequest).toHaveBeenLastCalledWith({
      endpoint: "/purchases",
      request,
    });

    await purchasesPOST(request);
    expect(routeDependencies.proxyPurchasesRequest).toHaveBeenLastCalledWith({
      endpoint: "/purchases",
      request,
      method: "POST",
    });

    await purchaseOptionsGET(request);
    expect(routeDependencies.proxyPurchasesRequest).toHaveBeenLastCalledWith({
      endpoint: "/purchases/options",
      request,
    });
  });

  it("encodes purchase dynamic ids and action routes", async () => {
    const request = new Request("http://localhost:3000/api/purchases/PUR%201");
    const context = { params: Promise.resolve({ id: "PUR 1" }) };

    await purchaseGET(request, context);
    expect(routeDependencies.proxyPurchasesRequest).toHaveBeenLastCalledWith({
      endpoint: "/purchases/PUR%201",
      request,
    });

    await purchasePATCH(request, context);
    expect(routeDependencies.proxyPurchasesRequest).toHaveBeenLastCalledWith({
      endpoint: "/purchases/PUR%201",
      request,
      method: "PATCH",
    });

    await purchaseSubmitPOST(request, context);
    expect(routeDependencies.proxyPurchasesRequest).toHaveBeenLastCalledWith({
      endpoint: "/purchases/PUR%201/submit",
      request,
      method: "POST",
    });

    await purchaseCancelPOST(request, context);
    expect(routeDependencies.proxyPurchasesRequest).toHaveBeenLastCalledWith({
      endpoint: "/purchases/PUR%201/cancel",
      request,
      method: "POST",
    });
  });

  it("forwards supplier routes including archive", async () => {
    const request = new Request("http://localhost:3000/api/suppliers/SUP%201");
    const context = { params: Promise.resolve({ id: "SUP 1" }) };

    await suppliersGET(request);
    expect(routeDependencies.proxyPurchasesRequest).toHaveBeenLastCalledWith({
      endpoint: "/suppliers",
      request,
    });

    await suppliersPOST(request);
    expect(routeDependencies.proxyPurchasesRequest).toHaveBeenLastCalledWith({
      endpoint: "/suppliers",
      request,
      method: "POST",
    });

    await supplierGET(request, context);
    expect(routeDependencies.proxyPurchasesRequest).toHaveBeenLastCalledWith({
      endpoint: "/suppliers/SUP%201",
      request,
    });

    await supplierPATCH(request, context);
    expect(routeDependencies.proxyPurchasesRequest).toHaveBeenLastCalledWith({
      endpoint: "/suppliers/SUP%201",
      request,
      method: "PATCH",
    });

    await supplierDELETE(request, context);
    expect(routeDependencies.proxyPurchasesRequest).toHaveBeenLastCalledWith({
      endpoint: "/suppliers/SUP%201",
      request,
      method: "DELETE",
    });
  });
});
