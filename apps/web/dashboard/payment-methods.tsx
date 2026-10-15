import type { Catalog } from "@api/types";

type PaymentMethod = NonNullable<Catalog["payment_methods"]>[number];

export function paymentMethodLabel(method: PaymentMethod) {
  const percent = method.discount_percent;
  return /^0+(?:\.0+)?$/.test(percent)
    ? method.label
    : `${method.label} (−${percent.replace(".", ",")}%)`;
}

export function PaymentMethodChooser({
  methods,
  value,
  disabled,
  onChange,
}: {
  methods: PaymentMethod[];
  value: PaymentMethod["id"] | null;
  disabled: boolean;
  onChange: (method: PaymentMethod["id"]) => void;
}) {
  return (
    <fieldset disabled={disabled} className="payment-methods">
      <legend>Способ оплаты</legend>
      {methods.length === 0 && (
        <p className="notice">Способы оплаты временно недоступны.</p>
      )}
      {methods.map((method) => (
        <label key={method.id} className="payment-method-choice">
          <input
            type="radio"
            name="payment_method"
            value={method.id}
            checked={value === method.id}
            disabled={!method.available}
            onChange={() => onChange(method.id)}
          />
          <span className="payment-method-copy">
            {paymentMethodLabel(method)}
            {!method.available && (
              <span className="payment-method-reason">
                {method.unavailable_reason || "Временно недоступен"}
              </span>
            )}
          </span>
        </label>
      ))}
    </fieldset>
  );
}
