import { beforeEach, describe, expect, it, vi } from "vitest";

const routeDependencies = vi.hoisted(() => ({
  proxyPOSRequest: vi.fn(),
}));

vi.mock("@/lib/pos-proxy", () => ({
  proxyPOSRequest: routeDependencies.proxyPOSRequest,
}));

import { GET as heldGET, POST as heldPOST } from "@/app/api/pos/held-receipts/route";
import {
  DELETE as heldDELETE,
  GET as heldDetailGET,
  PATCH as heldPATCH,
} from "@/app/api/pos/held-receipts/[heldId]/route";
import { GET as optionsGET } from "@/app/api/pos/options/route";
import { GET as productsGET } from "@/app/api/pos/products/route";
import { GET as salesGET, POST as salesPOST } from "@/app/api/pos/sales/route";
import { GET as saleDetailGET } from "@/app/api/pos/sales/[saleId]/route";
import { GET as currentShiftGET } from "@/app/api/pos/shifts/current/route";
import { POST as shiftsPOST } from "@/app/api/pos/shifts/route";
import { POST as shiftClosePOST } from "@/app/api/pos/shifts/[shiftId]/close/route";

beforeEach(() => {
  routeDependencies.proxyPOSRequest.mockReset().mockResolvedValue(new Response(null));
});

describe("POS route handlers", () => {
  it("forwards POS collection routes to the POS proxy", async () => {
    const request = new Request("http://localhost:3000/api/pos/sales?q=milk");

    await optionsGET(request);
    expect(routeDependencies.proxyPOSRequest).toHaveBeenLastCalledWith({
      endpoint: "/pos/options",
      request,
    });

    await productsGET(request);
    expect(routeDependencies.proxyPOSRequest).toHaveBeenLastCalledWith({
      endpoint: "/pos/products",
      request,
    });

    await currentShiftGET(request);
    expect(routeDependencies.proxyPOSRequest).toHaveBeenLastCalledWith({
      endpoint: "/pos/shifts/current",
      request,
    });

    await shiftsPOST(request);
    expect(routeDependencies.proxyPOSRequest).toHaveBeenLastCalledWith({
      endpoint: "/pos/shifts",
      request,
      method: "POST",
    });

    await heldGET(request);
    expect(routeDependencies.proxyPOSRequest).toHaveBeenLastCalledWith({
      endpoint: "/pos/held-receipts",
      request,
    });

    await heldPOST(request);
    expect(routeDependencies.proxyPOSRequest).toHaveBeenLastCalledWith({
      endpoint: "/pos/held-receipts",
      request,
      method: "POST",
    });

    await salesGET(request);
    expect(routeDependencies.proxyPOSRequest).toHaveBeenLastCalledWith({
      endpoint: "/pos/sales",
      request,
    });

    await salesPOST(request);
    expect(routeDependencies.proxyPOSRequest).toHaveBeenLastCalledWith({
      endpoint: "/pos/sales",
      request,
      method: "POST",
    });
  });

  it("encodes dynamic ids for shift close, held receipts and sale details", async () => {
    const request = new Request("http://localhost:3000/api/pos/held-receipts/HELD%201");

    await shiftClosePOST(request, { params: Promise.resolve({ shiftId: "SHIFT 1" }) });
    expect(routeDependencies.proxyPOSRequest).toHaveBeenLastCalledWith({
      endpoint: "/pos/shifts/SHIFT%201/close",
      request,
      method: "POST",
    });

    await heldDetailGET(request, { params: Promise.resolve({ heldId: "HELD 1" }) });
    expect(routeDependencies.proxyPOSRequest).toHaveBeenLastCalledWith({
      endpoint: "/pos/held-receipts/HELD%201",
      request,
    });

    await heldPATCH(request, { params: Promise.resolve({ heldId: "HELD 1" }) });
    expect(routeDependencies.proxyPOSRequest).toHaveBeenLastCalledWith({
      endpoint: "/pos/held-receipts/HELD%201",
      request,
      method: "PATCH",
    });

    await heldDELETE(request, { params: Promise.resolve({ heldId: "HELD 1" }) });
    expect(routeDependencies.proxyPOSRequest).toHaveBeenLastCalledWith({
      endpoint: "/pos/held-receipts/HELD%201",
      request,
      method: "DELETE",
    });

    await saleDetailGET(request, { params: Promise.resolve({ saleId: "SALE 1" }) });
    expect(routeDependencies.proxyPOSRequest).toHaveBeenLastCalledWith({
      endpoint: "/pos/sales/SALE%201",
      request,
    });
  });
});
