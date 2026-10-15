import { useId } from "react";
import type { Product } from "@api/types";
import { money, CatalogDescription, catalogText } from "./content";

export function CatalogComparison({ products }: { products: Product[] }) {
  const region = useId();
  if (!products.length) return null;
  const days = [
    ...new Set(
      products.flatMap((product) =>
        (product.periods ?? []).map((period) => period.days),
      ),
    ),
  ].sort((a, b) => a - b);
  return (
    <details className="catalog-comparison">
      <summary>Сравнить тарифы</summary>
      <p id={region} className="fine-print">
        Условия из актуального каталога. Итоговый расчёт с устройствами и
        скидками доступен перед оплатой.
      </p>
      <div
        className="comparison-scroll"
        role="region"
        aria-label="Сравнение тарифов"
        aria-describedby={region}
        tabIndex={0}
      >
        <table aria-label="Сравнение тарифов">
          <thead>
            <tr>
              <th scope="col">Условия</th>
              {products.map((product) => (
                <th key={product.id} scope="col">
                  {catalogText(product.name)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">Описание</th>
              {products.map((product) => (
                <td key={product.id}>
                  <CatalogDescription
                    nodes={product.content?.description}
                    text={product.description}
                  />
                </td>
              ))}
            </tr>
            <tr>
              <th scope="row">Устройств включено</th>
              {products.map((product) => (
                <td key={product.id}>{product.included_devices}</td>
              ))}
            </tr>
            <tr>
              <th scope="row">Базовый период</th>
              {products.map((product) => (
                <td key={product.id}>
                  {product.base_period_days
                    ? `${product.base_period_days} дней · `
                    : "Базовый срок не задан · "}
                  {money(product.price, product.currency)}
                </td>
              ))}
            </tr>
            {days.map((day) => (
              <tr key={day}>
                <th scope="row">{day} дней</th>
                {products.map((product) => {
                  const period = (product.periods ?? []).find(
                    (item) => item.days === day,
                  );
                  return (
                    <td key={product.id}>
                      {period
                        ? money(period.price, product.currency)
                        : "Недоступно"}
                    </td>
                  );
                })}
              </tr>
            ))}
            <tr>
              <th scope="row">Дополнительное устройство</th>
              {products.map((product) => (
                <td key={product.id}>
                  {product.extra_device_price === null
                    ? "Стоимость не указана"
                    : money(product.extra_device_price, product.currency)}
                </td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>
    </details>
  );
}
