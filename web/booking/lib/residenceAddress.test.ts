/**
 * Runnable with: node --experimental-strip-types --test lib/residenceAddress.test.ts
 * (Node 22+) — pure helper regression for compose/split/save resolve.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  addressPatchDecision,
  buildGuestPatchPayload,
  composeResidenceAddress,
  resolveAddressForSave,
  resolveLocalAddress,
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

describe("addressPatchDecision (B-fix)", () => {
  it("omits while city-only incomplete", () => {
    assert.deepEqual(addressPatchDecision("Osijek", ""), { kind: "omit" });
  });

  it("omits while street-only incomplete", () => {
    assert.deepEqual(addressPatchDecision("", "Floragatan 44"), { kind: "omit" });
  });

  it("sets composed when both complete", () => {
    assert.deepEqual(addressPatchDecision("Osijek", "Floragatan 44"), {
      kind: "set",
      address: "Osijek, Floragatan 44",
    });
  });

  it("sets empty clear when both empty", () => {
    assert.deepEqual(addressPatchDecision("", ""), { kind: "set", address: "" });
    assert.deepEqual(addressPatchDecision("  ", "  "), { kind: "set", address: "" });
  });
});

describe("buildGuestPatchPayload", () => {
  const form = {
    first_name: "Ada",
    last_name: "Diag",
    address: "Zagreb, Ilica 15",
  };

  it("omits address key on partial city→street", () => {
    const body = buildGuestPatchPayload(form, "Osijek", "");
    assert.equal("address" in body, false);
    assert.equal(body.first_name, "Ada");
  });

  it("omits address key on partial street→city", () => {
    const body = buildGuestPatchPayload(form, "", "Floragatan 44");
    assert.equal("address" in body, false);
  });

  it("includes new address when complete — does not keep Zagreb", () => {
    const body = buildGuestPatchPayload(form, "Osijek", "Floragatan 44");
    assert.equal(body.address, "Osijek, Floragatan 44");
  });

  it("includes empty address when both cleared", () => {
    const body = buildGuestPatchPayload(form, "", "");
    assert.equal(body.address, "");
  });
});

describe("resolveLocalAddress", () => {
  it("keeps previous locally while incomplete (UI only; PATCH omits)", () => {
    assert.equal(
      resolveLocalAddress("Osijek", "", "Zagreb, Ilica 15"),
      "Zagreb, Ilica 15",
    );
  });

  it("updates when complete", () => {
    assert.equal(
      resolveLocalAddress("Osijek", "Floragatan 44", "Zagreb, Ilica 15"),
      "Osijek, Floragatan 44",
    );
  });
});

describe("resolveAddressForSave (legacy keep-previous)", () => {
  it("uses compose when complete", () => {
    assert.equal(
      resolveAddressForSave("Osijek", "Dubrovačka 30", "old"),
      "Osijek, Dubrovačka 30",
    );
  });

  it("documents legacy keep-previous (do not use for PATCH)", () => {
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
