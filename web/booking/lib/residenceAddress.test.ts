/**
 * Runnable with: node --experimental-strip-types --test lib/residenceAddress.test.ts
 * (Node 22+) — pure helper regression for compose/split/save resolve.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  composeResidenceAddress,
  resolveAddressForSave,
  splitResidenceAddress,
} from "./residenceAddress";

describe("composeResidenceAddress", () => {
  it("composes when both present", () => {
    assert.equal(composeResidenceAddress("Osijek", "Dubrovačka 30"), "Osijek, Dubrovačka 30");
  });

  it("returns empty when city missing", () => {
    assert.equal(composeResidenceAddress("", "Ilica 15"), "");
    assert.equal(composeResidenceAddress("  ", "Ilica 15"), "");
  });

  it("returns empty when street missing", () => {
    assert.equal(composeResidenceAddress("Zagreb", ""), "");
    assert.equal(composeResidenceAddress("Zagreb", "  "), "");
  });
});

describe("splitResidenceAddress", () => {
  it("splits on first comma", () => {
    assert.deepEqual(splitResidenceAddress("Zagreb, Ilica 15"), {
      city: "Zagreb",
      street: "Ilica 15",
    });
  });

  it("puts no-comma value in street only", () => {
    assert.deepEqual(splitResidenceAddress("Osijek Dubrovačka 30"), {
      city: "",
      street: "Osijek Dubrovačka 30",
    });
  });

  it("handles empty", () => {
    assert.deepEqual(splitResidenceAddress(""), { city: "", street: "" });
  });
});

describe("resolveAddressForSave", () => {
  it("uses compose when complete", () => {
    assert.equal(
      resolveAddressForSave("Osijek", "Dubrovačka 30", "old"),
      "Osijek, Dubrovačka 30",
    );
  });

  it("keeps previous while incomplete", () => {
    assert.equal(resolveAddressForSave("Osijek", "", "Zagreb, Ilica 15"), "Zagreb, Ilica 15");
  });

  it("clears when both empty", () => {
    assert.equal(resolveAddressForSave("", "", "Zagreb, Ilica 15"), "");
  });
});

describe("slot switch regression (split isolation)", () => {
  it("guest B replace does not retain guest A fragments", () => {
    const fromA = splitResidenceAddress("Zagreb, Ilica 15");
    assert.equal(fromA.city, "Zagreb");
    const fromB = splitResidenceAddress("Osijek, Dubrovačka 30");
    assert.deepEqual(fromB, { city: "Osijek", street: "Dubrovačka 30" });
    const fromEmpty = splitResidenceAddress("");
    assert.deepEqual(fromEmpty, { city: "", street: "" });
  });
});
