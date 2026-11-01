import { describe, expect, it } from "vitest";
import { centsToUsd, eventTone, flagLabel, messageKindLabel, ratio, reasonLabel, usd } from "./format";

describe("money display", () => {
  it("shows sub-dollar model costs with four decimals and prices with two", () => {
    expect(usd("0.016000")).toBe("$0.0160");
    expect(usd("5.0000")).toBe("$5.00");
    expect(usd(0)).toBe("$0.00");
    expect(usd(null)).toBe("—");
  });

  it("formats integer cents without floating point", () => {
    expect(centsToUsd(4300)).toBe("$43.00");
    expect(centsToUsd(123456)).toBe("$1,234.56");
    expect(centsToUsd(5)).toBe("$0.05");
  });

  it("clamps meter ratios", () => {
    expect(ratio("0.30", "0.20")).toBe(1);
    expect(ratio("0.05", "0.20")).toBe(0.25);
    expect(ratio("1", "0")).toBe(0);
  });
});

describe("labels", () => {
  it("translates system reasons into staff language", () => {
    expect(reasonLabel("held_budget")).toBe("Held at spend cap");
    expect(reasonLabel("something_new")).toBe("something new");
    expect(flagLabel("customer_language_es")).toBe("Customer wrote in ES");
  });

  it("marks rejected model output as a warning", () => {
    expect(eventTone("llm_call", { valid: false })).toBe("warn");
    expect(eventTone("escalated")).toBe("bad");
    expect(eventTone("work_order_created")).toBe("ok");
  });
});

describe("message kinds", () => {
  it("names every kind of message sent to a customer, and falls back to words", () => {
    expect(messageKindLabel("handoff")).toBe("Passed to the team");
    expect(messageKindLabel("staff_reply")).toBe("Staff reply");
    expect(messageKindLabel("something_new")).toBe("something new");
  });
});
