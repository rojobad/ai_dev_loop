import { BarChart, CustomChart, GaugeChart, TreemapChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import type { EChartsCoreOption, EChartsType } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import React, { useEffect, useRef } from "react";
import ReactEChartsCore from "echarts-for-react/lib/core";

echarts.use([
  BarChart,
  CustomChart,
  GaugeChart,
  TreemapChart,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
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
        option={option}
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
