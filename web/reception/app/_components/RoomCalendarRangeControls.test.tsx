import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { RoomCalendarRangeControls } from "@/app/_components/RoomCalendarRangeControls";
import { addDaysIso } from "@/lib/utils";
import hr from "@/messages/hr.json";

const FLOOR = "2026-08-13";
const HISTORY_MIN = "2025-08-13";

afterEach(() => {
  cleanup();
});

function Harness({
  initialRangeStart = FLOOR,
  initialHistory = false,
}: {
  initialRangeStart?: string;
  initialHistory?: boolean;
}) {
  const [rangeStart, setRangeStart] = useState(initialRangeStart);
  const [historyEnabled, setHistoryEnabled] = useState(initialHistory);
  return (
    <NextIntlClientProvider locale="hr" messages={hr}>
      <p data-testid="range-start">{rangeStart}</p>
      <RoomCalendarRangeControls
        rangeStart={rangeStart}
        rangeLabel={rangeStart}
        floor={FLOOR}
        historyMin={HISTORY_MIN}
        historyEnabled={historyEnabled}
        onRangeStartChange={setRangeStart}
        onHistoryEnabledChange={setHistoryEnabled}
      />
    </NextIntlClientProvider>
  );
}

function historyToggle() {
  return screen.getByRole("checkbox", { name: hr.calendar.historyAria });
}

function prevButton() {
  return screen.getByRole("button", { name: hr.calendar.prevPeriod });
}

function nextButton() {
  return screen.getByRole("button", { name: hr.calendar.nextPeriod });
}

function todayButton() {
  return screen.getByRole("button", { name: hr.calendar.today });
}

function rangeStart() {
  return screen.getByTestId("range-start").textContent;
}

describe("RoomCalendarRangeControls", () => {
  it("toggles the history checkbox and snaps back to the floor when unchecked in the past", () => {
    render(<Harness />);

    expect(historyToggle()).toHaveProperty("checked", false);

    fireEvent.click(historyToggle());
    expect(historyToggle()).toHaveProperty("checked", true);

    fireEvent.click(prevButton());
    expect(rangeStart()).toBe(addDaysIso(FLOOR, -30));

    fireEvent.click(historyToggle());
    expect(historyToggle()).toHaveProperty("checked", false);
    expect(rangeStart()).toBe(FLOOR);
  });

  it("locks prev on the floor when history is off", () => {
    render(<Harness />);
    expect(prevButton()).toHaveProperty("disabled", true);
    expect(nextButton()).toHaveProperty("disabled", false);
  });

  it("locks prev on historyMin when history is on", () => {
    render(<Harness initialHistory initialRangeStart={HISTORY_MIN} />);
    expect(prevButton()).toHaveProperty("disabled", true);
    expect(nextButton()).toHaveProperty("disabled", false);
  });

  it("clamps prev onto historyMin instead of crossing it", () => {
    render(<Harness initialHistory initialRangeStart={addDaysIso(HISTORY_MIN, 10)} />);
    fireEvent.click(prevButton());
    expect(rangeStart()).toBe(HISTORY_MIN);
    expect(prevButton()).toHaveProperty("disabled", true);
  });

  it("disables Today on the floor and returns to the floor from the past", () => {
    render(<Harness initialHistory />);
    expect(todayButton()).toHaveProperty("disabled", true);

    fireEvent.click(prevButton());
    expect(todayButton()).toHaveProperty("disabled", false);
    fireEvent.click(todayButton());
    expect(rangeStart()).toBe(FLOOR);
    expect(todayButton()).toHaveProperty("disabled", true);
  });

  it("shows the history prefix only after leaving the operational window", () => {
    render(<Harness />);
    fireEvent.click(historyToggle());
    expect(screen.queryByText(/Povijest ·/)).toBeNull();

    fireEvent.click(prevButton());
    expect(screen.getByText(`Povijest · ${addDaysIso(FLOOR, -30)}`)).toBeTruthy();
  });
});
