import type { POSLineInput, POSProduct, SaleLine } from "@/lib/pos";

export type CartLine = {
  product: POSProduct;
  quantity: string;
  discount_percent: string;
};

export function cartLineToInput(line: CartLine): POSLineInput {
  return {
    product_id: line.product.id,
    quantity: line.quantity,
    discount_percent: line.discount_percent,
  };
}

export function saleLineToCartLine(line: SaleLine, currency = "KZT"): CartLine {
  return {
    product: {
      id: line.product_id,
      sku: line.sku,
      name: line.name,
      barcode: null,
      unit: line.unit,
      sale_price: line.unit_price,
      currency,
      available: "0.000",
      is_active: true,
      allows_fractional_quantity: line.quantity.includes(".") && !line.quantity.endsWith(".000"),
    },
    quantity: line.quantity,
    discount_percent: line.discount_percent,
  };
}
