import React, { forwardRef, useImperativeHandle, useState } from "react";
import { act, render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Mock } from "vitest";

interface ChartMock {
  dispose: Mock<() => void>;
  isDisposed: Mock<() => boolean>;
  resize: Mock<() => void>;
}

interface ObserverMock {
  callback: ResizeObserverCallback;
  disconnect: Mock<() => void>;
  observe: Mock<ResizeObserver["observe"]>;
}

const ownership = vi.hoisted(() => ({
  charts: [] as ChartMock[],
  observers: [] as ObserverMock[],
  props: [] as Array<Record<string, unknown>>,
}));

vi.mock("echarts/core", () => ({ use: vi.fn() }));
vi.mock("echarts/charts", () => ({
  BarChart: {},
  CustomChart: {},
  GaugeChart: {},
  HeatmapChart: {},
  SankeyChart: {},
  SunburstChart: {},
  TreemapChart: {},
}));
vi.mock("echarts/components", () => ({
  CalendarComponent: {},
  GridComponent: {},
  LegendComponent: {},
  TitleComponent: {},
  TooltipComponent: {},
  VisualMapComponent: {},
}));
vi.mock("echarts/renderers", () => ({ CanvasRenderer: {} }));
vi.mock("echarts-for-react/lib/core", () => ({
  default: forwardRef(function MockChart(props: object, ref: React.ForwardedRef<object>) {
    ownership.props.push(props as Record<string, unknown>);
    const [chart] = useState<ChartMock>(() => {
      let disposed = false;
      const instance: ChartMock = {
        resize: vi.fn(),
        dispose: vi.fn(() => {
          disposed = true;
        }),
        isDisposed: vi.fn(() => disposed),
      };
      ownership.charts.push(instance);
      return instance;
    });
    useImperativeHandle(ref, () => ({ getEchartsInstance: () => chart }));
    return <div data-testid="echarts" />;
  }),
}));

import {
  DashboardChart,
  viewportTooltipPosition,
  withViewportTooltip,
} from "./DashboardChart";

beforeEach(() => {
  ownership.charts.length = 0;
  ownership.observers.length = 0;
  ownership.props.length = 0;
  class ResizeObserverMock {
    readonly record: ObserverMock;

    constructor(callback: ResizeObserverCallback) {
      this.record = { callback, observe: vi.fn(), disconnect: vi.fn() };
      ownership.observers.push(this.record);
    }

    observe = (...args: Parameters<ResizeObserver["observe"]>) => this.record.observe(...args);
    unobserve = vi.fn();
    disconnect = () => this.record.disconnect();
  }
  vi.stubGlobal("ResizeObserver", ResizeObserverMock);
});

describe("chart ownership", () => {
  it("resizes the current instance and retires replaced and unmounted ownership", () => {
    const view = render(
      <DashboardChart ariaLabel="owned chart" height={200} option={{}} theme="light" />,
    );
    expect(ownership.charts).toHaveLength(1);
    expect(ownership.observers).toHaveLength(1);
    expect(ownership.observers[0].observe).toHaveBeenCalledTimes(1);

    act(() => ownership.observers[0].callback([], {} as ResizeObserver));
    expect(ownership.charts[0].resize).toHaveBeenCalledTimes(1);

    view.rerender(
      <DashboardChart
        ariaLabel="owned chart"
        height={200}
        option={{}}
        ownershipKey="new selection"
        theme="dark"
      />,
    );
    expect(ownership.charts).toHaveLength(2);
    expect(ownership.observers).toHaveLength(2);
    expect(ownership.observers[0].disconnect).toHaveBeenCalledTimes(1);
    expect(ownership.charts[0].dispose).toHaveBeenCalledTimes(1);

    act(() => ownership.observers[0].callback([], {} as ResizeObserver));
    expect(ownership.charts[0].resize).toHaveBeenCalledTimes(1);
    act(() => ownership.observers[1].callback([], {} as ResizeObserver));
    expect(ownership.charts[1].resize).toHaveBeenCalledTimes(1);

    view.unmount();
    expect(ownership.observers[1].disconnect).toHaveBeenCalledTimes(1);
    expect(ownership.charts[1].dispose).toHaveBeenCalledTimes(1);
  });

  it("moves HTML tooltips to a body overlay and positions them in the viewport", () => {
    const formatter = vi.fn();
    const source = {
      tooltip: { renderMode: "html", formatter, extraCssText: "color:red" },
      series: [],
    };
    const result = withViewportTooltip(source) as {
      tooltip: Record<string, unknown>;
    };
    expect(result.tooltip).toMatchObject({
      appendToBody: true,
      confine: false,
      formatter,
    });
    expect(result.tooltip.extraCssText).toContain("max-width:min(32rem,calc(100vw - 16px))");
    expect(result.tooltip.extraCssText).toContain("z-index:900");
    expect(source.tooltip).not.toHaveProperty("appendToBody");
    expect(viewportTooltipPosition(
      [1350, 190],
      [281, 120],
      { left: -1060, top: 700 },
      { width: 390, height: 844 },
    )).toEqual([1161, 16]);

    render(
      <DashboardChart
        ariaLabel="tooltip chart"
        height={200}
        option={source}
        theme="light"
      />,
    );
    expect(ownership.props.at(-1)?.option).toMatchObject({
      tooltip: {
        appendToBody: true,
        confine: false,
        formatter,
        position: expect.any(Function),
      },
    });
  });
});
