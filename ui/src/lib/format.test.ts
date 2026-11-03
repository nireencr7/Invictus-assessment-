import { describe, expect, it } from "vitest";
import { centsToUsd, eventTone, flagLabel, jobTone, messageKindLabel, outcomeTone, ratio, reasonLabel, usd } from "./format";

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

describe("worker outcomes (dev page)", () => {
  it("colours what a worker run ended with, including escalations that carry a reason", () => {
    expect(outcomeTone("work_order_created")).toBe("ok");
    expect(outcomeTone("follow_up_asked")).toBe("ok");
    expect(outcomeTone("retry_scheduled")).toBe("warn");
    expect(outcomeTone("held_budget")).toBe("warn");
    expect(outcomeTone("escalated:customer_wrote_after_conversation_closed")).toBe("warn");
    expect(outcomeTone("dead_after_crash")).toBe("bad");
    expect(outcomeTone("already_processed")).toBe("info");
  });

  it("colours job statuses", () => {
    expect(jobTone("done")).toBe("ok");
    expect(jobTone("parked")).toBe("warn");
    expect(jobTone("dead")).toBe("bad");
    expect(jobTone("queued")).toBe("info");
  });
});
