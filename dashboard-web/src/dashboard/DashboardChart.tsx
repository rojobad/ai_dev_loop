import {
  BarChart,
  CustomChart,
  GaugeChart,
  HeatmapChart,
  SankeyChart,
  SunburstChart,
  TreemapChart,
} from "echarts/charts";
import {
  CalendarComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import type { EChartsCoreOption, EChartsType } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import React, { useEffect, useMemo, useRef } from "react";
import ReactEChartsCore from "echarts-for-react/lib/core";

echarts.use([
  BarChart,
  CustomChart,
  GaugeChart,
  HeatmapChart,
  SankeyChart,
  SunburstChart,
  TreemapChart,
  CalendarComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

export interface DashboardChartProps {
  ariaLabel: string;
  height: number;
  onEvents?: Record<string, (parameters: unknown) => void>;
  option: EChartsCoreOption;
  ownershipKey?: string;
  theme: "light" | "dark";
}

interface TooltipPositionSize {
  contentSize: [number, number];
}

type ChartBoundsProvider = () => Pick<DOMRect, "left" | "top"> | null;

const TOOLTIP_VIEWPORT_MARGIN_PX = 8;
const TOOLTIP_POINTER_GAP_PX = 12;

export function viewportTooltipPosition(
  point: [number, number],
  contentSize: [number, number],
  chartBounds: Pick<DOMRect, "left" | "top">,
  viewport: { width: number; height: number },
): [number, number] {
  const [contentWidth, contentHeight] = contentSize;
  const desiredViewportX = chartBounds.left + point[0] + TOOLTIP_POINTER_GAP_PX;
  const desiredViewportY = chartBounds.top + point[1] + TOOLTIP_POINTER_GAP_PX;
  const maximumViewportX = Math.max(
    TOOLTIP_VIEWPORT_MARGIN_PX,
    viewport.width - contentWidth - TOOLTIP_VIEWPORT_MARGIN_PX,
  );
  const maximumViewportY = Math.max(
    TOOLTIP_VIEWPORT_MARGIN_PX,
    viewport.height - contentHeight - TOOLTIP_VIEWPORT_MARGIN_PX,
  );
  const viewportX = Math.min(
    Math.max(TOOLTIP_VIEWPORT_MARGIN_PX, desiredViewportX),
    maximumViewportX,
  );
  const viewportY = Math.min(
    Math.max(TOOLTIP_VIEWPORT_MARGIN_PX, desiredViewportY),
    maximumViewportY,
  );
  return [viewportX - chartBounds.left, viewportY - chartBounds.top];
}

export function withViewportTooltip(
  option: EChartsCoreOption,
  chartBounds: ChartBoundsProvider = () => null,
): EChartsCoreOption {
  const root = option as Record<string, unknown>;
  const tooltip = root.tooltip;
  if (typeof tooltip !== "object" || tooltip === null || Array.isArray(tooltip)) return option;
  const tooltipRecord = tooltip as Record<string, unknown>;
  const existingCss = typeof tooltipRecord.extraCssText === "string"
    ? tooltipRecord.extraCssText
    : "";
  return {
    ...root,
    tooltip: {
      ...tooltipRecord,
      appendToBody: true,
      confine: false,
      position: (
        point: [number, number],
        _parameters: unknown,
        _element: HTMLElement,
        _rect: unknown,
        size: TooltipPositionSize,
      ) => {
        const bounds = chartBounds();
        if (!bounds || typeof window === "undefined") return point;
        return viewportTooltipPosition(point, size.contentSize, bounds, {
          width: window.innerWidth,
          height: window.innerHeight,
        });
      },
      extraCssText: `${existingCss};max-width:min(32rem,calc(100vw - 16px));white-space:normal;overflow-wrap:anywhere;z-index:900;`,
    },
  } as EChartsCoreOption;
}

export function DashboardChart({
  ariaLabel,
  height,
  onEvents,
  option,
  ownershipKey = "shared",
  theme,
}: DashboardChartProps): React.JSX.Element {
  const wrapper = useRef<HTMLDivElement>(null);
  const component = useRef<ReactEChartsCore>(null);
  const viewportOption = useMemo(
    () => withViewportTooltip(option, () => wrapper.current?.getBoundingClientRect() ?? null),
    [option],
  );

  useEffect(() => {
    const instance = component.current?.getEchartsInstance() as EChartsType | undefined;
    const element = wrapper.current;
    if (!instance || !element) return;
    const observer = new ResizeObserver(() => {
      if (!instance.isDisposed()) instance.resize();
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
      if (!instance.isDisposed()) instance.dispose();
    };
  }, [ownershipKey, theme]);

  return (
    <div ref={wrapper} className="chart-frame" role="img" aria-label={ariaLabel}>
      <ReactEChartsCore
        key={`${theme}:${ownershipKey}`}
        ref={component}
        echarts={echarts}
        option={viewportOption}
        onEvents={onEvents}
        notMerge
        lazyUpdate
        opts={{ renderer: "canvas" }}
        style={{ height, width: "100%" }}
        theme={theme === "dark" ? "dark" : undefined}
      />
    </div>
  );
}
