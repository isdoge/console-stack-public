package collector

import (
	"context"
	"testing"
	"time"

	"github.com/shirou/gopsutil/v3/net"
)

func TestNICMapFiltersLoopback(t *testing.T) {
	stats := []net.IOCountersStat{
		{Name: "lo", BytesSent: 100, BytesRecv: 100},
		{Name: "lo0", BytesSent: 100, BytesRecv: 100},
		{Name: "enp1s0", BytesSent: 1000, BytesRecv: 2000},
		{Name: "tailscale0", BytesSent: 500, BytesRecv: 600},
	}
	m := NICMap(stats)
	if _, ok := m["lo"]; ok {
		t.Fatal("lo 应被过滤")
	}
	if _, ok := m["lo0"]; ok {
		t.Fatal("lo0 应被过滤")
	}
	if m["enp1s0"].SentBytesTotal != 1000 || m["enp1s0"].RecvBytesTotal != 2000 {
		t.Fatalf("enp1s0 计数器错误: %+v", m["enp1s0"])
	}
	// tailscale0 不过滤：计数器层保留全部物理/虚拟网卡，计费挑选归接收端/账本规则。
	if _, ok := m["tailscale0"]; !ok {
		t.Fatal("tailscale0 应保留在计数器中")
	}
}

func TestComputeRates(t *testing.T) {
	hc := NewHostCollector(time.Second, "")
	t0 := time.Now().UTC()

	// 首份样本：无差分基准。
	cur := map[string]NICCounters{"eth0": {SentBytesTotal: 1000, RecvBytesTotal: 2000}}
	if r := hc.computeRates(cur, t0); r != nil {
		t.Fatalf("首份样本不应有速率: %+v", r)
	}
	hc.prevNICs, hc.prevAt = cur, t0

	// 第二份样本：差分速率 = Δbytes / Δt。
	t1 := t0.Add(10 * time.Second)
	cur2 := map[string]NICCounters{"eth0": {SentBytesTotal: 3000, RecvBytesTotal: 6000}}
	r := hc.computeRates(cur2, t1)
	if r == nil {
		t.Fatal("应有速率")
	}
	if r.UpBytesSec["eth0"] != 200 {
		t.Fatalf("up 速率错误: %v（期望 200 bytes/s）", r.UpBytesSec["eth0"])
	}
	if r.DownBytesSec["eth0"] != 400 {
		t.Fatalf("down 速率错误: %v（期望 400 bytes/s）", r.DownBytesSec["eth0"])
	}

	// 计数器变小（重启归零）：该网卡跳过，不产生负速率。
	hc.prevNICs, hc.prevAt = cur2, t1
	cur3 := map[string]NICCounters{"eth0": {SentBytesTotal: 5, RecvBytesTotal: 5}}
	if r := hc.computeRates(cur3, t1.Add(10*time.Second)); r != nil {
		t.Fatalf("计数器归零后不应有速率: %+v", r)
	}

	// 时间差非正：跳过。
	hc.prevNICs, hc.prevAt = cur2, t1
	if r := hc.computeRates(cur2, t1); r != nil {
		t.Fatalf("同刻样本不应有速率: %+v", r)
	}
}

// TestSampleOnThisHost 在测试机上真实采集一轮：验证主机采集器在本平台可用，
// 且字段满足不变量（used = total - available、百分比在 0–100）。
func TestSampleOnThisHost(t *testing.T) {
	hc := NewHostCollector(100*time.Millisecond, "")
	hs, errs, err := hc.Sample(context.Background(), time.Now().UTC())
	if err != nil {
		t.Skipf("本机采集不可用（环境限制）: %v", err)
	}
	if hs == nil {
		t.Fatal("样本不应为 nil")
	}
	if hs.MemTotalBytes > 0 {
		if hs.MemUsedBytes != hs.MemTotalBytes-hs.MemAvailableBytes {
			t.Fatalf("内存守恒破坏: used=%d total=%d available=%d",
				hs.MemUsedBytes, hs.MemTotalBytes, hs.MemAvailableBytes)
		}
	}
	if hs.DiskUsedPct < 0 || hs.DiskUsedPct > 100 {
		t.Fatalf("磁盘百分比越界: %v", hs.DiskUsedPct)
	}
	if hs.CPUUsedPct < 0 || hs.CPUUsedPct > 100 {
		t.Fatalf("CPU 百分比越界: %v", hs.CPUUsedPct)
	}
	// 部分失败（如容器平台无 load）记入 errs 是合法的，但 cpu/mem 应尽力可用。
	t.Logf("errs=%v cpu=%v mem_avail=%d", errs, hs.CPUUsedPct, hs.MemAvailableBytes)
}
