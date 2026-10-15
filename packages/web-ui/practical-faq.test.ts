import { expect, it } from "vitest";
import { addPracticalFaq } from "./practical-faq";

it("adds troubleshooting starters once without replacing owner-edited published answers", () => {
  const original = [
    {
      id: "help_vpn_connection",
      question: "Ответ владельца",
      answer: [
        {
          type: "paragraph",
          children: [
            { type: "text", text: "Настроенная владельцем инструкция" },
          ],
        },
      ],
    },
  ];
  const result = addPracticalFaq(original);
  expect(result.find((item) => item.id === "help_vpn_connection")).toEqual(
    original[0],
  );
  expect(result.some((item) => item.id === "help_payment_access")).toBe(true);
  expect(result.some((item) => item.id === "help_device_limit")).toBe(true);
  expect(addPracticalFaq(result)).toEqual(result);
  expect(original).toHaveLength(1);
});

it("respects the existing public-content limit while preserving every configured question", () => {
  const current = Array.from({ length: 29 }, (_, i) => ({
    id: `existing-${i}`,
    question: `Вопрос ${i}`,
    answer: [],
  }));
  const result = addPracticalFaq(current);
  expect(result).toHaveLength(30);
  expect(result.slice(0, 29)).toEqual(current);
});
