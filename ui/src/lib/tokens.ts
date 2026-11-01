/** Staff tokens from whatever was pasted into the sign-in box: the riverbend-staff-tokens secret
 *  as gcloud prints it ({"<token>": "<shop>", …}, survey nag and all), or bare tokens separated by
 *  spaces, commas or new lines. */
export function parseTokens(text: string): string[] {
  const start = text.indexOf("{");
  let tokens: string[];
  if (start !== -1) {
    const notSecret = new Error("That looks like the staff-tokens secret but isn't complete JSON. Paste it exactly as gcloud printed it.");
    const end = text.lastIndexOf("}");
    if (end < start) throw notSecret;
    let parsed: unknown;
    try {
      parsed = JSON.parse(text.slice(start, end + 1));
    } catch {
      throw notSecret;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw notSecret;
    tokens = Object.keys(parsed);
  } else {
    tokens = text.split(/[\s,]+/).map((t) => t.replace(/^["']+|["']+$/g, "")).filter(Boolean);
  }
  return [...new Set(tokens)];
}
