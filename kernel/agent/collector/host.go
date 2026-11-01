package collector

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/shirou/gopsutil/v3/cpu"
	"github.com/shirou/gopsutil/v3/disk"
	"github.com/shirou/gopsutil/v3/host"
	"github.com/shirou/gopsutil/v3/load"
	"github.com/shirou/gopsutil/v3/mem"
	"github.com/shirou/gopsutil/v3/net"
)

// HostCollector 采集主机指标，并在相邻样本间维护网卡计数器差分状态。
type HostCollector struct {
	prevNICs    map[string]NICCounters // 上一份成功样本的网卡计数器，用于差分速率
	prevAt      time.Time              // 上一份成功样本的采样时刻
	statsWindow time.Duration          // CPU 利用率测量窗（与采样间隔对齐）
	diskPath    string                 // 磁盘使用率采集挂载点（B6-1 裁决：默认 /，多盘机器显式配置主数据盘）
}

// NewHostCollector 创建主机采集器。statsWindow 是 CPU 百分比测量窗；
// diskPath 是磁盘使用率采集的挂载点（空则默认 /）。
func NewHostCollector(statsWindow time.Duration, diskPath string) *HostCollector {
	if diskPath == "" {
		diskPath = "/"
	}
	return &HostCollector{prevNICs: map[string]NICCounters{}, statsWindow: statsWindow, diskPath: diskPath}
}

// Sample 采集一轮主机指标。部分失败不丢弃整份样本：失败的采集器记入 errs。
// 全部采集器失败返回 error，此时样本不进缓冲。
func (hc *HostCollector) Sample(ctx context.Context, at time.Time) (*HostSample, map[string]string, error) {
	hs := &HostSample{}
	errs := map[string]string{}

	if cpuPct, err := cpu.PercentWithContext(ctx, hc.statsWindow, false); err != nil || len(cpuPct) == 0 {
		errs["cpu"] = errMsg(err, "空结果")
	} else {
		hs.CPUUsedPct = round1(cpuPct[0])
	}

	if vm, err := mem.VirtualMemory(); err != nil {
		errs["mem"] = err.Error()
	} else {
		hs.MemTotalBytes = vm.Total
		hs.MemAvailableBytes = vm.Available
		hs.MemUsedBytes = vm.Total - vm.Available
	}
	if sw, err := mem.SwapMemory(); err != nil {
		errs["swap"] = err.Error()
	} else {
		hs.SwapTotalBytes = sw.Total
		hs.SwapUsedBytes = sw.Used
	}

	if du, err := disk.UsageWithContext(ctx, hc.diskPath); err != nil {
		errs["disk"] = err.Error()
	} else {
		hs.DiskTotalBytes = du.Total
		hs.DiskUsedBytes = du.Used
		hs.DiskUsedPct = round1(du.UsedPercent)
	}

	if la, err := load.Avg(); err != nil {
		errs["load"] = err.Error()
	} else {
		hs.LoadAvg1m, hs.LoadAvg5m, hs.LoadAvg15m = la.Load1, la.Load5, la.Load15
	}

	if up, err := host.UptimeWithContext(ctx); err != nil {
		errs["uptime"] = err.Error()
	} else {
		hs.UptimeSec = up
	}

	if nics, err := net.IOCountersWithContext(ctx, true); err != nil {
		errs["net"] = err.Error()
	} else {
		hs.NICs = NICMap(nics)
		if rates := hc.computeRates(hs.NICs, at); rates != nil {
			hs.Rates = rates
		}
		hc.prevNICs, hc.prevAt = hs.NICs, at
	}

	if len(errs) == 7 {
		return nil, nil, fmt.Errorf("主机采集全部失败: %v", errs)
	}
	return hs, errs, nil
}

// computeRates 用上一份成功样本差分出逐网卡速率。
// 首份样本、时间差非正、或某网卡计数器变小（重启/归零）时跳过该网卡，不产生负速率。
func (hc *HostCollector) computeRates(cur map[string]NICCounters, at time.Time) *NICRates {
	if len(hc.prevNICs) == 0 || !hc.prevAt.Before(at) {
		return nil
	}
	dt := at.Sub(hc.prevAt).Seconds()
	if dt <= 0 {
		return nil
	}
	up, down := map[string]float64{}, map[string]float64{}
	for name, c := range cur {
		p, ok := hc.prevNICs[name]
		if !ok {
			continue // 上一份不存在的网卡（新接口）无差分基准
		}
		if c.SentBytesTotal < p.SentBytesTotal || c.RecvBytesTotal < p.RecvBytesTotal {
			continue // 计数器变小 = 归零重启/换基线，该样本窗丢弃
		}
		up[name] = round1(float64(c.SentBytesTotal-p.SentBytesTotal) / dt)
		down[name] = round1(float64(c.RecvBytesTotal-p.RecvBytesTotal) / dt)
	}
	if len(up) == 0 {
		return nil
	}
	return &NICRates{UpBytesSec: up, DownBytesSec: down}
}

// NICMap 把 gopsutil 的 IOCountersStat 列表转成 payload 的 NICs map（过滤环回）。
func NICMap(stats []net.IOCountersStat) map[string]NICCounters {
	m := map[string]NICCounters{}
	for _, s := range stats {
		if s.Name == "lo" || strings.HasPrefix(s.Name, "lo0") {
			continue
		}
		m[s.Name] = NICCounters{SentBytesTotal: s.BytesSent, RecvBytesTotal: s.BytesRecv}
	}
	return m
}

func round1(v float64) float64 {
	if v < 0 {
		return v
	}
	return float64(int(v*10+0.5)) / 10
}

func errMsg(err error, fallback string) string {
	if err != nil {
		return err.Error()
	}
	return fallback
}
