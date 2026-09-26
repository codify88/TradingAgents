import { describe, expect, it } from "vitest";
import { remaining } from "./format";

describe("remaining", () => {
  it("reads like a person would say it", () => {
    expect(remaining(0)).toBe("closed");
    expect(remaining(59)).toBe("1 min left");
    expect(remaining(36 * 60)).toBe("36 min left");
    expect(remaining(65 * 60)).toBe("1 h 5 min left");
    expect(remaining((59 * 60 + 43) * 60)).toBe("2 d 11 h left");
  });
});
