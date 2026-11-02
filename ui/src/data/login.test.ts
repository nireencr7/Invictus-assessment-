import { describe, expect, it } from "vitest";
import { sessionFromLogin, type LoginResponse } from "./api";

const response: LoginResponse = {
  token: "rbs_personal",
  expires_at: "2026-11-03T05:00:00Z",
  account: { email: "sam@riverbend.example", name: "Sam" },
  shops: [
    { id: "rb-downtown", name: "Riverbend Cycles Downtown" },
    { id: "rb-lakeshore", name: "Riverbend Cycles Lakeshore" },
  ],
};

describe("email sign-in", () => {
  it("opens every shop of the account with the one personal token", () => {
    const s = sessionFromLogin("", response);
    expect(s.shops).toEqual([
      { id: "rb-downtown", name: "Riverbend Cycles Downtown", token: "rbs_personal" },
      { id: "rb-lakeshore", name: "Riverbend Cycles Lakeshore", token: "rbs_personal" },
    ]);
    expect(s.account).toEqual({ email: "sam@riverbend.example", name: "Sam", token: "rbs_personal", expiresAt: "2026-11-03T05:00:00Z" });
  });

  it("keeps an admin token only when one was given", () => {
    expect(sessionFromLogin("", response).adminToken).toBeUndefined();
    expect(sessionFromLogin("", response, "adm").adminToken).toBe("adm");
  });

  it("says so when the account has no shop yet", () => {
    expect(() => sessionFromLogin("", { ...response, shops: [] })).toThrow(/isn't linked to any shop/);
  });
});
