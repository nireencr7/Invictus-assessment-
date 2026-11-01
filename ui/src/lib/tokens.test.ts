import { describe, expect, it } from "vitest";
import { parseTokens } from "./tokens";

describe("sign-in token parsing", () => {
  it("reads the tokens out of the secret exactly as gcloud prints it", () => {
    const pasted = '{"tok-a":"rb-downtown","tok-b":"rb-lakeshore","tok-c":"rb-northside"}\n\n'
      + "To take a quick anonymous survey, run:\n  $ gcloud survey\n";
    expect(parseTokens(pasted)).toEqual(["tok-a", "tok-b", "tok-c"]);
  });

  it("takes bare tokens split by lines, spaces or commas, with stray quotes", () => {
    expect(parseTokens("tok-a\n tok-b, tok-c")).toEqual(["tok-a", "tok-b", "tok-c"]);
    expect(parseTokens('"tok-a"')).toEqual(["tok-a"]);
  });

  it("drops duplicates and blanks", () => {
    expect(parseTokens("tok-a tok-a\n\n")).toEqual(["tok-a"]);
    expect(parseTokens("   ")).toEqual([]);
  });

  it("says so when the secret was only partly copied", () => {
    expect(() => parseTokens('{"tok-a":"rb-downtown","tok-b"')).toThrow(/isn't complete JSON/);
    expect(() => parseTokens('{"tok-a" "rb-downtown"}')).toThrow(/isn't complete JSON/);
  });
});
